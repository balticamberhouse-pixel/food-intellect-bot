# bot.py — AI-редактор канала «Пищевой интеллект» v7: рубрики, наука, КБЖУ, точные картинки
import os, json, time, uuid, datetime, subprocess, logging, random
from urllib.parse import quote
import requests, urllib3
urllib3.disable_warnings()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = (os.environ.get("BOT_TOKEN") or os.environ.get("TOKEN") or "").strip()
API = "https://api.telegram.org/bot" + BOT_TOKEN + "/"
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "210"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
DEFAULT_STATE = {"owner": 0, "channel": "", "done": {}, "pending": None,
                 "topics": [], "offset": 0, "setup_warned": "", "gen_count": 0}
STATE = dict(DEFAULT_STATE)
try:
    with open(STATE_FILE, encoding="utf-8") as f:
        STATE.update(json.load(f))
except Exception:
    pass

PROVIDER = os.environ.get("LLM_PROVIDER", "gigachat")
GIGA_KEY = os.environ.get("GIGACHAT_KEY", "").strip()
GIGA_MODEL = os.environ.get("GIGACHAT_MODEL", "GigaChat-Pro")
OR_KEY = os.environ.get("OPENROUTER_KEY", "").strip()
OR_MODEL = os.environ.get("OPENROUTER_MODEL", "openrouter/free")
PEX_KEY = os.environ.get("PEXELS_KEY", "").strip()

CHANGED = False
def save_state():
    global CHANGED
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)
    CHANGED = True

def cur_provider(): return STATE.get("provider") or PROVIDER
def cur_model(): return STATE.get("giga_model") or GIGA_MODEL
def cur_ormodel(): return STATE.get("or_model") or OR_MODEL
def cur_imgsource(): return STATE.get("imgsource") or ("pexels" if PEX_KEY else "ai")

SLOTS = {"morning": (9, 0), "evening": (16, 0)}
LABEL = {"morning": "09:00", "evening": "16:00", "test": "тест"}

RUBRICS = [
    ("Разбор этикетки", "как читать состав и КБЖУ на упаковке: сахар, трансжиры, соли, Е-добавки, уловки маркетологов; учишь замечать скрытое."),
    ("КБЖУ и расчёты", "калорийность и баланс БЖУ: формула Миффлина-Сан Жеора (женщины: 10*вес_кг + 6.25*рост_см - 5*возраст - 161; мужчины: то же + 5), "
                        "коэффициенты активности 1.2 / 1.375 / 1.55 / 1.725, дефицит и профицит, метод тарелки; обязательно приведи полный пример расчёта с цифрами."),
    ("Миф против науки", "популярный пищевой миф и что на самом деле показывают исследования: называешь миф, затем данные науки с автором и годом, затем вывод."),
    ("Обзор исследования", "одно-два реальных известных исследования о питании или образе жизни: суть простыми словами, цифра из вывода, как применить в жизни."),
    ("Хранение и готовка", "как хранить и готовить продукты, чтобы сохранить пользу и не навредить: сроки, температура, ошибки, которые стоят здоровья."),
    ("Планирование рациона", "составление меню и заготовок на неделю: список покупок, бюджет, batch-cooking, заморозка, готовые схемы приёмов пищи."),
    ("ЗОЖ шире еды", "сон, физическая активность, вода, стресс и привычки: как они влияют на вес и здоровье, с ссылками на исследования и конкретными нормами."),
    ("Сезонность и составы", "сезонные продукты и сравнение похожих продуктов между собой: что выбрать и почему, с цифрами по составу и цене."),
]

def msk():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)

def tg(method, _http_timeout=25, **kw):
    for _ in range(2):
        try:
            r = requests.post(API + method, json=kw, timeout=_http_timeout)
            if r.ok:
                return r.json().get("result")
            log.warning("TG %s -> HTTP %s: %s", method, r.status_code, r.text[:200])
            if r.status_code == 429:
                time.sleep(int(r.json().get("parameters", {}).get("retry_after", 3))); continue
            if 400 <= r.status_code < 500:
                return {"error": r.json().get("description", r.text[:150])}
        except Exception as e:
            log.warning("TG %s exception: %s", method, e)
        time.sleep(2)
    return None

def ok_res(res):
    return bool(res) and not (isinstance(res, dict) and "error" in res)

def notify_owner(text):
    if STATE["owner"]:
        res = tg("sendMessage", chat_id=STATE["owner"], text=text)
        log.info("notify_owner -> %s", "ok" if ok_res(res) else res)

def cyr_ratio(s):
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    ru = sum(1 for c in letters if ("а" <= c.lower() <= "я") or c.lower() == "ё")
    return ru / len(letters)

# ---------- нейросеть ----------
_g = {"tok": None, "exp": 0.0}
def giga_token():
    if _g["tok"] and time.time() < _g["exp"] - 60:
        return _g["tok"]
    r = requests.post("https://gigachat.devices.sberbank.ru/api/v2/oauth",
        headers={"Authorization": "Basic " + GIGA_KEY,
                 "RqUID": str(uuid.uuid4()),
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"scope": "GIGACHAT_API_PERS"}, verify=False, timeout=30)
    r.raise_for_status()
    d = r.json()
    _g["tok"] = d["access_token"]; _g["exp"] = d["expires_at"] / 1000.0
    return _g["tok"]

SYSTEM = ("Ты — профессиональный редактор телеграм-канала «Пищевой интеллект». Тематика канала — весь спектр "
          "здорового образа жизни: питание и выбор продуктов, чтение этикеток, КБЖУ и расчёт рациона, готовка и хранение, "
          "физическая активность, сон, вода, стресс и привычки. Пиши СТРОГО НА РУССКОМ ЯЗЫКЕ, содержательно и по-деловому, "
          "но живо: хук в первой строке, короткие предложения, обращение на «ты», конкретные цифры и примеры. "
          "Упоминай только РЕАЛЬНЫЕ широко известные исследования (автор или коллектив, год, вывод одной фразой) и только "
          "если уверен в них; перефразируй выводы, НЕ выдумывай цитаты, DOI и точные цифры, в которых не уверен; "
          "где уместно — «проконсультируйтесь с врачом». Без воды, штампов и маркдауна (без звёздочек и решёток).")

def llm_text(user_prompt):
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_prompt}]
    if cur_provider() == "openrouter":
        r = requests.post("https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": "Bearer " + OR_KEY},
            json={"model": cur_ormodel(), "messages": msgs}, timeout=90)
    else:
        r = requests.post("https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={"Authorization": "Bearer " + giga_token()},
            json={"model": cur_model(), "messages": msgs, "temperature": 0.9},
            verify=False, timeout=90)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def build_prompt(rubric_name, rubric_hint, comment):
    recent = "\n".join("- " + t for t in STATE["topics"][-20:]) or "- (пока пусто)"
    p = ("Рубрика этого поста: «" + rubric_name + "». " + rubric_hint +
         "\nПридумай НОВУЮ тему в рамках рубрики, которой ещё не было (недавние темы — не повторяй):\n" + recent +
         "\n\nСтруктура поста обязательная:\n"
         "1) Первая строка — хук: неожиданный факт, цифра или разоблачение.\n"
         "2) 2-3 абзаца сути с конкретикой и цифрами.\n"
         "3) Блок «🔬 Что говорит наука:» — 1-2 предложения с упоминанием реального известного исследования "
         "(автор/коллектив, год) и его вывода простыми словами.\n"
         "4) Блок «✅ Что делать:» — 2-3 конкретных шага или расчёт с цифрами (в рубрике КБЖУ — полный пример расчёта "
         "по формуле Миффлина-Сан Жеора с коэффициентом активности).\n"
         "5) Финал — резкий вывод и вопрос читателям.\n"
         "6) 3-4 хэштега по-русски с новой строки.\n\n"
         "Формат ответа строго такой, без вступлений и без маркдауна:\n"
         "1-я строка: ТЕМА: <тема в 3-6 словах по-русски>\n"
         "2-я строка: КАРТИНКА: <3-6 слов НА АНГЛИЙСКОМ: конкретные предметы в кадре, точно соответствующие теме поста, "
         "например 'raw salmon fillet with vegetables on white plate'>\n"
         "Затем пост целиком, НЕ БОЛЕЕ 1000 символов. Никаких пояснений вне формата.")
    if comment:
        p += "\n\nКомментарий редактора (владельца канала), который надо учесть: " + comment
    return p

TRANSLATE_PROMPT = ("Переведи на русский язык пост для телеграм-канала, сохранив структуру, хук, цифры и смысл. "
                    "Хэштеги тоже по-русски. Первая строка ответа: «ТЕМА: <тема по-русски в 3-6 словах>», "
                    "затем пост. Верни только текст без пояснений:\n\n")

def clean_line(s):
    return s.strip().lstrip("*#> ").strip()

def split_topic(raw):
    topic, img_desc, body = "", "", []
    for i, raw_line in enumerate(raw.strip().splitlines()):
        line = clean_line(raw_line)
        if not line:
            continue
        up = line.upper()
        if not topic and i < 6 and up.startswith("ТЕМА:"):
            topic = line.split(":", 1)[1].strip()
            continue
        if not img_desc and i < 6 and up.startswith(("КАРТИНКА:", "IMAGE:", "PICTURE:")):
            img_desc = line.split(":", 1)[1].strip()
            continue
        body.append(line)
    rest = "\n".join(body).strip()
    if len(rest) > 1000:
        rest = rest[:1000].rsplit("\n", 1)[0] + "\n…"
    return topic or "(тема)", img_desc, rest

# ---------- картинки ----------
def pexels_image_url(desc):
    r = requests.get("https://api.pexels.com/v1/search",
        headers={"Authorization": PEX_KEY},
        params={"query": desc, "per_page": 5, "orientation": "landscape"}, timeout=30)
    r.raise_for_status()
    photos = r.json().get("photos") or []
    if not photos:
        return None
    return random.choice(photos[:5])["src"]["large"]

def make_image_url(desc):
    q = quote(desc + ", professional magazine food photography, soft natural window light, "
                     "shallow depth of field, rustic kitchen table, appetizing, ultra detailed, "
                     "no text, no watermark")
    return ("https://image.pollinations.ai/prompt/" + q +
            "?width=1024&height=768&model=flux&nologo=true&enhance=true")

def pick_image(desc):
    src = cur_imgsource()
    if src == "pexels" and PEX_KEY:
        try:
            url = pexels_image_url(desc)
            if url:
                log.info("image: pexels query=%s", desc)
                return url
        except Exception as e:
            log.warning("pexels failed: %s", e)
    log.info("image: ai query=%s", desc)
    return make_image_url(desc)

def generate(slot, comment=""):
    idx = int(STATE.get("gen_count", 0))
    STATE["gen_count"] = idx + 1
    rubric_name, rubric_hint = RUBRICS[idx % len(RUBRICS)]
    log.info("rubric: %s", rubric_name)
    try:
        raw = llm_text(build_prompt(rubric_name, rubric_hint, comment))
    except Exception as e:
        body = getattr(getattr(e, "response", None), "text", "")[:300]
        log.error("LLM error: %s | body: %s", e, body)
        notify_owner("⚠️ Не удалось сгенерировать пост (" + str(e)[:150] + "). Попробую в следующий запуск.")
        return
    topic, img_desc, text = split_topic(raw)
    if cyr_ratio(text) < 0.5:
        log.warning("non-russian draft, translating")
        try:
            fixed = llm_text(TRANSLATE_PROMPT + text)
            t2, d2, x2 = split_topic(fixed)
            if cyr_ratio(x2) >= 0.5:
                topic, text = t2 or topic, x2
                img_desc = img_desc or d2
        except Exception:
            log.exception("translate failed")
    if not img_desc:
        img_desc = "delicious healthy fresh food close-up"
    img = pick_image(img_desc)
    STATE["pending"] = {"slot": slot, "topic": topic, "text": text, "image": img,
                        "created": int(time.time()), "reminded": int(time.time())}
    save_state()
    pic = tg("sendPhoto", chat_id=STATE["owner"], photo=img, caption=text)
    if not ok_res(pic):
        notify_owner("📝 Черновик к " + LABEL.get(slot, slot) + " (без картинки):\n\n" + text)
    notify_owner("──────────────\nРубрика: " + rubric_name +
                 "\nЧто дальше:\n🖼 Пришлите своё фото — заменю картинку\n"
                 "✅ /publish — опубликовать\n🔄 /redo комментарий — переписать\n⏭️ /skip — пропустить")

# ---------- публикация ----------
def post_link(mid):
    ch = STATE["channel"]
    if ch.startswith("@"):
        return "https://t.me/" + ch[1:] + "/" + str(mid)
    if ch.startswith("-100"):
        return "https://t.me/c/" + ch[4:] + "/" + str(mid)
    return ""

def publish():
    p = STATE["pending"]
    if not p:
        notify_owner("Сейчас нет черновика. /test — сделать тестовый."); return
    if not STATE["channel"]:
        notify_owner("Канал не настроен. Пришлите /channel @ваш_канал"); return
    res = None
    if p.get("image"):
        res = tg("sendPhoto", chat_id=STATE["channel"], photo=p["image"], caption=p["text"])
        if not ok_res(res):
            log.warning("photo publish failed, fallback to text")
    if not ok_res(res):
        res = tg("sendMessage", chat_id=STATE["channel"], text=p["text"])
    if not ok_res(res):
        err = res.get("error", "нет ответа") if isinstance(res, dict) else "нет ответа"
        notify_owner("⚠️ Не удалось опубликовать: " + err +
                     "\nПроверьте, что бот — администратор канала с правом «Публиковать сообщения».")
        return
    if p["slot"] in SLOTS:
        STATE["done"][p["slot"]] = msk().date().isoformat()
    STATE["topics"] = (STATE["topics"] + [p["topic"]])[-40:]
    STATE["pending"] = None
    save_state()
    link = post_link(res.get("message_id"))
    notify_owner("✅ Опубликовано в канал!" + ("\n" + link if link else ""))

# ---------- команды владельца ----------
def menu():
    return ("Мои команды:\n/publish — опубликовать черновик\n"
            "/pub + фото с подписью-текстом — опубликовать ВАШ пост сразу\n"
            "/redo комментарий — переписать черновик\n/skip — пропустить публикацию\n"
            "/test — тестовый черновик прямо сейчас\n/channel @имя — задать канал\n"
            "/imgsource pexels|ai — источник картинок (фото или генерация)\n"
            "/ormodel имя — закрепить модель OpenRouter\n"
            "/status — состояние\n/done morning|evening — пометить слот сделанным")

def status_text():
    today = msk().date().isoformat()
    d = STATE["done"]
    nxt = RUBRICS[int(STATE.get("gen_count", 0)) % len(RUBRICS)][0]
    return ("Статус:\nКанал: " + (STATE["channel"] or "не настроен") +
            "\nНейросеть: " + cur_provider() + " / " + (cur_model() if cur_provider() == "gigachat" else cur_ormodel()) +
            "\nКартинки: " + cur_imgsource() +
            "\nСледующая рубрика: " + nxt +
            "\nУтро (09:00): " + ("готово" if d.get("morning") == today else "ожидает") +
            "\nВечер (16:00): " + ("готово" if d.get("evening") == today else "ожидает") +
            "\nЧерновик на проверке: " + ("да («" + STATE["pending"]["topic"] + "»)" if STATE["pending"] else "нет"))

def handle(msg):
    if msg.get("chat", {}).get("type") != "private":
        return
    uid = msg["chat"]["id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if STATE["owner"] == 0:
        if text.startswith("/start"):
            STATE["owner"] = uid; save_state()
            notify_owner("👋 Привет! Я — AI-редактор «Пищевого интеллекта».\n"
                         "Пришлите /channel @имя_канала, затем /test — сделаю пробный черновик с картинкой.\n"
                         "Дальше буду работать сам: черновики в 09:00 и 16:00 МСК.")
        else:
            tg("sendMessage", chat_id=uid, text="Я жду владельца канала. Отправьте /start.")
        return
    if uid != STATE["owner"]:
        return
    low = text.lower()
    if msg.get("photo"):
        if low.startswith("/pub"):
            body = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
            if not STATE["channel"]:
                notify_owner("Канал не настроен: /channel @имя_канала"); return
            res = tg("sendPhoto", chat_id=STATE["channel"], photo=msg["photo"][-1]["file_id"], caption=body)
            if ok_res(res):
                link = post_link(res.get("message_id"))
                notify_owner("✅ Опубликовано!" + ("\n" + link if link else ""))
            else:
                notify_owner("⚠️ Не удалось опубликовать: " + (res.get("error") if isinstance(res, dict) else "нет ответа"))
            return
        if STATE["pending"]:
            STATE["pending"]["image"] = msg["photo"][-1]["file_id"]; save_state()
            notify_owner("🖼 Фото получил — заменю картинку поста. /publish — опубликовать.")
        else:
            notify_owner("Черновика нет. Можно опубликовать напрямую: пришлите фото с подписью, где первая строка /pub, а дальше текст поста.")
        return
    if low.startswith("/publish") or low in ("опубликуй", "опубликовать"):
        publish()
    elif low.startswith("/redo") or low.startswith("переделай"):
        parts = text.split(None, 1)
        comment = parts[1] if len(parts) > 1 else ""
        slot = STATE["pending"]["slot"] if STATE["pending"] else "test"
        notify_owner("🔄 Переписываю…"); generate(slot, comment)
    elif low.startswith("/skip"):
        p = STATE["pending"]
        if p and p["slot"] in SLOTS:
            STATE["done"][p["slot"]] = msk().date().isoformat()
        STATE["pending"] = None; save_state(); notify_owner("⏭️ Пропустил публикацию.")
    elif low.startswith("/test"):
        generate("test")
    elif low.startswith("/imgsource"):
        parts = text.split(None, 1)
        if len(parts) > 1 and parts[1] in ("pexels", "ai"):
            STATE["imgsource"] = parts[1]; save_state()
            notify_owner("Источник картинок: " + parts[1] + (" (нужен секрет PEXELS_KEY)" if parts[1] == "pexels" and not PEX_KEY else ""))
    elif low.startswith("/channel"):
        parts = text.split(None, 1)
        if len(parts) > 1:
            ch = parts[1].strip()
            if not ch.startswith("@") and not ch.startswith("-100"):
                ch = "@" + ch
            STATE["channel"] = ch; save_state()
            info = tg("getChat", chat_id=ch)
            if ok_res(info):
                notify_owner("✅ Канал подключён: " + str(info.get("title", ch)))
            else:
                notify_owner("Сохранил канал " + ch + ", но не смог открыть. Проверьте имя и что бот добавлен админом.")
        else:
            notify_owner("Формат: /channel @имя_канала")
    elif low.startswith("/model"):
        parts = text.split(None, 1)
        if len(parts) > 1:
            STATE["giga_model"] = parts[1].strip(); save_state(); notify_owner("Модель: " + STATE["giga_model"])
    elif low.startswith("/ormodel"):
        parts = text.split(None, 1)
        if len(parts) > 1:
            STATE["or_model"] = parts[1].strip(); save_state(); notify_owner("Модель OpenRouter: " + STATE["or_model"])
    elif low.startswith("/provider"):
        parts = text.split(None, 1)
        if len(parts) > 1 and parts[1] in ("gigachat", "openrouter"):
            STATE["provider"] = parts[1]; save_state(); notify_owner("Провайдер: " + parts[1])
    elif low.startswith("/status"):
        notify_owner(status_text())
    elif low.startswith("/done"):
        parts = text.split()
        if len(parts) > 1 and parts[1] in SLOTS:
            STATE["done"][parts[1]] = msk().date().isoformat(); save_state()
            notify_owner("Пометил слот «" + LABEL[parts[1]] + "» сделанным на сегодня.")
    elif low.startswith("/start") or low.startswith("/help") or low == "меню":
        notify_owner(menu())
    else:
        notify_owner("Я понимаю только команды. /help — список команд.")

# ---------- расписание ----------
def setup_problem():
    if cur_provider() == "gigachat" and not GIGA_KEY:
        return "в репозитории не задан секрет GIGACHAT_KEY"
    if cur_provider() == "openrouter" and not OR_KEY:
        return "в репозитории не задан секрет OPENROUTER_KEY"
    if not STATE["channel"]:
        return "не задан канал (пришлите /channel @…)"
    return None

def schedule_check():
    now = msk(); today = now.date().isoformat()
    for slot in ("morning", "evening"):
        h, m = SLOTS[slot]
        if STATE["done"].get(slot) == today or (now.hour, now.minute) < (h, m):
            continue
        p = STATE["pending"]
        if p:
            if p["slot"] == slot:
                if now.timestamp() - p["reminded"] >= 7200:
                    p["reminded"] = int(now.timestamp()); save_state()
                    notify_owner("⏰ Напоминаю: черновик к " + LABEL[slot] + " ждёт решения: /publish, /redo, /skip")
                continue
            if p["slot"] in SLOTS:
                STATE["done"][p["slot"]] = today
            notify_owner("⏭️ Черновик «" + p["topic"] + "» не был одобрен — пропускаю.")
            STATE["pending"] = None; save_state()
        prob = setup_problem()
        if prob:
            if STATE.get("setup_warned") != today:
                STATE["setup_warned"] = today; save_state()
                notify_owner("⚠️ Не могу поставить пост по расписанию: " + prob)
            continue
        log.info("generate %s", slot)
        generate(slot)

# ---------- сохранение в git ----------
def commit_state():
    if not CHANGED:
        return
    try:
        ref = os.environ.get("GITHUB_REF", "refs/heads/main")
        branch = ref.split("refs/heads/")[-1] if ref.startswith("refs/heads/") else "main"
        subprocess.run(["git", "config", "user.name", "food-intel-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "food-intel-bot@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", "state.json"], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", "state: auto update"], check=False)
            subprocess.run(["git", "push", "origin", "HEAD:" + branch], check=False)
            log.info("state pushed")
    except Exception as e:
        log.warning("commit failed: %s", e)

def main():
    if not BOT_TOKEN:
        log.error("НЕТ BOT_TOKEN! Добавьте секрет BOT_TOKEN в настройках репозитория.")
        return
    tg("deleteWebhook")
    me = tg("getMe")
    log.info("I am: %s", me)
    log.info("run started")
    end = time.time() + RUN_SECONDS
    while time.time() < end:
        ups = tg("getUpdates", _http_timeout=55, offset=STATE["offset"], timeout=50, allowed_updates=["message"]) or []
        if isinstance(ups, dict):
            ups = []; time.sleep(3)
        for u in ups:
            STATE["offset"] = u["update_id"] + 1; save_state()
            m = u.get("message") or {}
            log.info("update %s | chat %s (%s) | %s", u["update_id"],
                     m.get("chat", {}).get("id"), m.get("chat", {}).get("type"),
                     (m.get("text") or m.get("caption") or "<фото>")[:80])
            try:
                if m:
                    handle(m)
            except Exception:
                log.exception("handle")
        try:
            schedule_check()
        except Exception:
            log.exception("sched")
    commit_state()
    log.info("run finished")

if __name__ == "__main__":
    main()
