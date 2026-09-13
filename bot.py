# bot.py — AI-редактор канала «Пищевой интеллект» (версия для GitHub Actions)
import os, json, time, uuid, datetime, subprocess, logging
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
                 "topics": [], "offset": 0, "setup_warned": ""}
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

SLOTS = {"morning": (9, 0), "evening": (16, 0)}
LABEL = {"morning": "09:00", "evening": "16:00", "test": "тест"}

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

def notify_owner(text):
    if STATE["owner"]:
        tg("sendMessage", chat_id=STATE["owner"], text=text)

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

SYSTEM = ("Ты — главный редактор телеграм-канала «Пищевой интеллект». Канал о питании и еде: "
          "как выбирать продукты, читать составы, хранить и готовить, мифы и факты о питании, "
          "планирование рациона, сезонные продукты, разумная экономия без вреда для здоровья. "
          "Пиши по-русски, экспертно, но простым языком, без выдуманных исследований и точных цифр, "
          "без медицинских назначений (где уместно — добавляй «проконсультируйтесь с врачом»). Без воды и штампов.")

def llm_text(user_prompt):
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_prompt}]
    if cur_provider() == "openrouter":
        r = requests.post("https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": "Bearer " + OR_KEY},
            json={"model": OR_MODEL, "messages": msgs}, timeout=180)
    else:
        r = requests.post("https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={"Authorization": "Bearer " + giga_token()},
            json={"model": cur_model(), "messages": msgs, "temperature": 0.9},
            verify=False, timeout=180)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

def build_prompt(comment):
    recent = "\n".join("- " + t for t in STATE["topics"][-20:]) or "- (пока пусто)"
    p = ("Придумай НОВУЮ тему поста, которой ещё не было (недавние темы — не повторяй):\n" + recent +
         "\n\nНапиши готовый пост для канала.\nФормат:\nПервая строка: «ТЕМА: <тема в 3-6 словах>»\n"
         "Затем сам пост: цепляющий заголовок с 1 эмодзи, 3-4 коротких абзаца с практической пользой, "
         "строка «💡 Вывод: …», в конце 3-4 хэштега с новой строки.\n"
         "Всего НЕ БОЛЕЕ 950 символов. Пиши только текст поста, без пояснений.")
    if comment:
        p += "\n\nКомментарий редактора (владельца канала), который надо учесть: " + comment
    return p

def split_topic(raw):
    lines = raw.strip().splitlines()
    topic, rest = "", raw.strip()
    if lines and lines[0].upper().startswith("ТЕМА:"):
        topic = lines[0].split(":", 1)[1].strip()
        rest = "\n".join(lines[1:]).strip()
    if len(rest) > 990:
        rest = rest[:990].rsplit("\n", 1)[0] + "\n…"
    return topic or "(тема)", rest

def generate(slot, comment=""):
    try:
        raw = llm_text(build_prompt(comment))
    except Exception as e:
        log.exception("LLM error")
        notify_owner("⚠️ Не удалось сгенерировать пост (" + str(e)[:200] + "). Попробую в следующий запуск.")
        return
    topic, text = split_topic(raw)
    STATE["pending"] = {"slot": slot, "topic": topic, "text": text, "image": None,
                        "created": int(time.time()), "reminded": int(time.time())}
    save_state()
    notify_owner("📝 Черновик к " + LABEL.get(slot, slot) + " готов.\n\n" + text +
                 "\n\n──────────────\nЧто дальше (отвечаю в течение ~5 минут):\n"
                 "🖼 Пришлите фото — прикреплю его к посту\n✅ /publish — опубликовать в канал\n"
                 "🔄 /redo ваш комментарий — переписать\n⏭️ /skip — пропустить публикацию")

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
    if p["image"]:
        res = tg("sendPhoto", chat_id=STATE["channel"], photo=p["image"], caption=p["text"])
    else:
        res = tg("sendMessage", chat_id=STATE["channel"], text=p["text"])
    if not res or (isinstance(res, dict) and "error" in res):
        err = res.get("error", "нет ответа Telegram") if isinstance(res, dict) else "нет ответа"
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
    return ("Мои команды (отвечаю в течение ~5 минут):\n/publish — опубликовать черновик\n"
            "/redo комментарий — переписать черновик\n/skip — пропустить публикацию\n"
            "/test — тестовый черновик прямо сейчас\n/channel @имя — задать канал\n"
            "/model имя — модель (GigaChat-Pro, GigaChat-Max, GigaChat-3-Ultra)\n"
            "/status — состояние\n/done morning|evening — пометить слот сделанным")

def status_text():
    today = msk().date().isoformat()
    d = STATE["done"]
    return ("Статус:\nКанал: " + (STATE["channel"] or "не настроен") +
            "\nНейросеть: " + cur_provider() + " / " + cur_model() +
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
                         "Пришлите /channel @имя_канала, затем /test — сделаю пробный черновик.\n"
                         "Дальше буду работать сам: черновики в 09:00 и 16:00 МСК.")
        else:
            tg("sendMessage", chat_id=uid, text="Я жду владельца канала. Отправьте /start.")
        return
    if uid != STATE["owner"]:
        return
    low = text.lower()
    if msg.get("photo"):
        if STATE["pending"]:
            STATE["pending"]["image"] = msg["photo"][-1]["file_id"]; save_state()
            notify_owner("🖼 Фото получил. /publish — опубликовать пост с ним.")
        else:
            notify_owner("Сейчас нет черновика в ожидании. /test — сделать черновик.")
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
    elif low.startswith("/channel"):
        parts = text.split(None, 1)
        if len(parts) > 1:
            ch = parts[1].strip()
            if not ch.startswith("@") and not ch.startswith("-100"):
                ch = "@" + ch
            STATE["channel"] = ch; save_state()
            info = tg("getChat", chat_id=ch)
            if isinstance(info, dict) and "error" not in info:
                notify_owner("✅ Канал подключён: " + str(info.get("title", ch)))
            else:
                notify_owner("Сохранил канал " + ch + ", но не смог открыть. Проверьте имя и что бот добавлен админом.")
        else:
            notify_owner("Формат: /channel @имя_канала")
    elif low.startswith("/model"):
        parts = text.split(None, 1)
        if len(parts) > 1:
            STATE["giga_model"] = parts[1].strip(); save_state(); notify_owner("Модель: " + STATE["giga_model"])
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
    log.info("run started")
    end = time.time() + RUN_SECONDS
    while time.time() < end:
        ups = tg("getUpdates", _http_timeout=55, offset=STATE["offset"], timeout=50, allowed_updates=["message"]) or []
        if isinstance(ups, dict):
            ups = []; time.sleep(3)
        for u in ups:
            STATE["offset"] = u["update_id"] + 1; save_state()
            try:
                if "message" in u:
                    handle(u["message"])
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
