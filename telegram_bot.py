"""Telegram-бот для HabitFlow.

Связывает Telegram с REST API v1 трекера (long polling, только стандартная
библиотека — внешних зависимостей нет).

Настройка через переменные окружения:
    TELEGRAM_BOT_TOKEN    токен бота от @BotFather (обязательно)
    HABITFLOW_URL         адрес трекера (по умолчанию http://127.0.0.1:5000)
    HABITFLOW_API_KEY     ключ по умолчанию (необязательно; иначе /link <key>)

Ключ берётся на странице /integrations в приложении.

Запуск (при работающем app.py):
    python telegram_bot.py
"""

import os
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API_BASE = "https://api.telegram.org/bot" + BOT_TOKEN
HABITFLOW_URL = os.environ.get("HABITFLOW_URL", "http://127.0.0.1:5000").rstrip("/")
DEFAULT_KEY = os.environ.get("HABITFLOW_API_KEY", "")
LINKS_FILE = os.environ.get(
    "HABITFLOW_LINKS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_links.json"),
)

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# --------------------------------------------------------------------------
# Хранилище привязок chat_id -> api_key
# --------------------------------------------------------------------------

def load_links():
    try:
        with open(LINKS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_links(links):
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(links, f, ensure_ascii=False, indent=2)


def key_for(chat_id):
    links = load_links()
    return links.get(str(chat_id)) or DEFAULT_KEY


# --------------------------------------------------------------------------
# HTTP: трекер (HabitFlow REST API v1)
# --------------------------------------------------------------------------

def hf_request(path, key, method="GET", payload=None):
    url = HABITFLOW_URL + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-API-Key": key or "",
        "Content-Type": "application/json",
        "User-Agent": "HabitFlow-Telegram/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def hf_get(path, key):
    return hf_request(path, key, "GET")


def hf_post(path, key, payload=None):
    return hf_request(path, key, "POST", payload or {})


# --------------------------------------------------------------------------
# HTTP: Telegram Bot API
# --------------------------------------------------------------------------

def tg(method, params=None, timeout=60):
    url = API_BASE + "/" + method
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send(chat_id, text, keyboard=None):
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if keyboard is not None:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("sendMessage", params)


# --------------------------------------------------------------------------
# Формирование представлений
# --------------------------------------------------------------------------

def scheduled_today(habit):
    days = habit.get("days") or list(range(7))
    return date.today().weekday() in days


def render_today(key):
    status, data = hf_get("/api/v1/habits", key)
    if status == 401:
        return None, None
    if status != 200:
        return "⚠️ Не удалось получить привычки. Проверьте, запущен ли трекер.", None

    habits = [h for h in data.get("habits", []) if scheduled_today(h)]
    today_iso = date.today().isoformat()
    if not habits:
        return "На сегодня нет запланированных привычек. Добавьте: /add Название", None

    lines = [f"📅 <b>Сегодня — {date.today().strftime('%d.%m.%Y')}</b>", ""]
    keyboard = []
    done = 0
    for h in habits:
        mark = "✅" if h["done_today"] else "⬜"
        if h["done_today"]:
            done += 1
        lines.append(f"{mark} {h['icon']} {h['title']}  🔥{h['streak']}")
        keyboard.append([{
            "text": f"{mark} {h['title']}",
            "callback_data": "t:" + str(h["id"]),
        }])
    lines.append("")
    lines.append(f"Выполнено: {done}/{len(habits)}")
    return "\n".join(lines), keyboard


def render_habits(key):
    status, data = hf_get("/api/v1/habits", key)
    if status == 401:
        return None
    if status != 200:
        return "⚠️ Трекер недоступен."
    habits = data.get("habits", [])
    if not habits:
        return "Привычек пока нет. Добавьте: /add Название"
    out = ["📋 <b>Все привычки</b>", ""]
    for h in habits:
        days = "".join(WEEKDAYS[d] for d in h["days"] if 0 <= d < 7) or "—"
        out.append(f"{h['icon']} <b>{h['title']}</b> · 🔥{h['streak']} (рекорд {h['best_streak']}) · {days}")
    return "\n".join(out)


def render_stats(key, period):
    status, data = hf_get("/api/v1/stats?period=" + period, key)
    if status == 401:
        return None
    if status != 200:
        return "⚠️ Не удалось получить статистику."
    names = {"week": "Неделя", "month": "Месяц", "year": "Год"}
    out = [f"📊 <b>Статистика — {names.get(period, period)}</b>", ""]
    out.append(f"Общий процент: <b>{data['overall_percent']}%</b>")
    out.append(f"Выполнено: {data['done']} · Пропущено: {data['missed']}")
    out.append("")
    for h in data.get("habits", []):
        out.append(f"{h['title']}: {h['percent']}% ({h['done']}/{h['expected']})")
    return "\n".join(out)


def render_coach(key):
    status, data = hf_get("/api/v1/coach", key)
    if status == 401:
        return None
    if status != 200:
        return "⚠️ Не удалось получить советы."
    out = ["🤖 <b>AI-коуч</b>", "", data.get("summary", "")]
    for ins in data.get("insights", []):
        out.append("")
        out.append(f"{ins['icon']} <b>{ins['title']}</b>")
        out.append(ins["text"])
    return "\n".join(out)


HELP = (
    "🤖 <b>HabitFlow-бот</b>\n\n"
    "• /today — привычки на сегодня и отметка\n"
    "• /habits — все привычки\n"
    "• /add Название — новая привычка\n"
    "• /stats [week|month|year] — статистика\n"
    "• /coach — советы AI-коуча\n"
    "• /link &lt;API-ключ&gt; — привязать аккаунт и включить напоминания\n"
    "• /chatid — показать ваш chat ID\n"
    "• /unlink — отвязать аккаунт\n\n"
    "Ключ берётся в приложении на странице «Интеграции»."
)


# --------------------------------------------------------------------------
# Обработка команд и callback
# --------------------------------------------------------------------------

def handle_command(chat_id, text):
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]
    key = key_for(chat_id)

    if cmd in ("/start", "/help"):
        send(chat_id, HELP)
        return

    if cmd == "/link":
        if len(parts) < 2:
            send(chat_id, "Использование: <code>/link ВАШ_API_КЛЮЧ</code>")
            return
        candidate = parts[1].strip()
        status, _ = hf_get("/api/v1/me", candidate)
        if status == 200:
            links = load_links()
            links[str(chat_id)] = candidate
            save_links(links)
            # Включаем серверную push-рассылку в этот чат.
            lstatus, info = hf_post("/api/v1/telegram/link", candidate, {"chat_id": chat_id})
            tip = ""
            if lstatus == 200:
                if info.get("bot_configured"):
                    tip = ("\n🔔 Напоминания в Telegram включены (в "
                           + str(info.get("reminder_time", "20:00")) + ").")
                else:
                    tip = ("\n⚠️ Трекер запущен без токена бота — напоминания будут "
                           "работать после перезапуска с <code>TELEGRAM_BOT_TOKEN</code>.")
            send(chat_id, "✅ Аккаунт привязан." + tip + "\nОткройте /today")
        else:
            send(chat_id, "❌ Неверный ключ. Проверьте его на странице «Интеграции».")
        return

    if cmd == "/chatid":
        send(chat_id, f"Ваш chat ID: <code>{chat_id}</code>")
        return

    if cmd == "/unlink":
        links = load_links()
        old = links.pop(str(chat_id), None)
        save_links(links)
        if old:
            hf_request("/api/v1/telegram/link", old, method="DELETE")
        send(chat_id, "Аккаунт отвязан. Напоминания отключены.")
        return

    if not key:
        send(chat_id, "Сначала привяжите аккаунт: <code>/link ВАШ_API_КЛЮЧ</code>")
        return

    if cmd == "/today":
        text, keyboard = render_today(key)
        if text is None:
            send(chat_id, "Сессия недействительна. Привяжите аккаунт: /link")
            return
        send(chat_id, text, keyboard)
    elif cmd == "/habits":
        msg = render_habits(key)
        send(chat_id, msg or "Сессия недействительна. /link")
    elif cmd == "/add":
        title = " ".join(parts[1:]).strip()
        if not title:
            send(chat_id, "Использование: <code>/add Пить воду</code>")
            return
        status, _ = hf_post("/api/v1/habits", key, {"title": title})
        if status == 201:
            send(chat_id, f"✅ Привычка «{title}» создана.")
        else:
            send(chat_id, "⚠️ Не удалось создать привычку.")
    elif cmd == "/stats":
        period = parts[1].lower() if len(parts) > 1 else "week"
        if period not in ("week", "month", "year"):
            period = "week"
        msg = render_stats(key, period)
        send(chat_id, msg or "Сессия недействительна. /link")
    elif cmd == "/coach":
        msg = render_coach(key)
        send(chat_id, msg or "Сессия недействительна. /link")
    else:
        send(chat_id, "Неизвестная команда. /help")


def handle_update(update):
    if "message" in update and update["message"].get("text"):
        msg = update["message"]
        handle_command(msg["chat"]["id"], msg["text"])
    elif "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        _handle_callback_with_message(
            cq["id"], chat_id, cq.get("data", ""), cq["message"]["message_id"]
        )


def _handle_callback_with_message(callback_id, chat_id, data, message_id):
    tg("answerCallbackQuery", {"callback_query_id": callback_id})
    key = key_for(chat_id)
    if not key or not data.startswith("t:"):
        return
    habit_id = data[2:]
    status, habit = hf_get(f"/api/v1/habits/{habit_id}", key)
    if status != 200:
        send(chat_id, "⚠️ Привычка не найдена.")
        return
    new_done = not habit["done_today"]
    status, res = hf_post(f"/api/v1/habits/{habit_id}/check", key, {"done": new_done})
    if status != 200:
        send(chat_id, "⚠️ Не удалось отметить.")
        return
    flash = ("🔥 Серия: " + str(res["streak"])) if new_done else "Отменено"
    for b in res.get("new_badges", []):
        flash += "  🏆 " + b["title"]
    tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": flash})
    text, keyboard = render_today(key)
    if text:
        tg("editMessageText", {
            "chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "reply_markup": {"inline_keyboard": keyboard or []},
        })


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------

def check_connection():
    """Проверяет доступ к Telegram Bot API и печатает понятную диагностику."""
    me = tg("getMe", {}, timeout=10)
    if me.get("ok"):
        r = me.get("result", {})
        print("Бот: @" + str(r.get("username")) + "  (" + str(r.get("first_name")) + ")")
        print("Ссылка: https://t.me/" + str(r.get("username")))
        return True
    print("=" * 60)
    print("НЕ УДАЛОСЬ СВЯЗАТЬСЯ С api.telegram.org")
    print("Детали:", me.get("error"))
    print("")
    print("Скорее всего доступ к Telegram Bot API заблокирован провайдером")
    print("или файрволом. Варианты решения:")
    print("  1) Включите VPN, покрывающий этот компьютер, и запустите снова.")
    print("  2) Если есть HTTP/SOCKS-прокси, задайте переменную окружения:")
    print("       $env:HTTPS_PROXY='http://логин:пароль@host:port'")
    print("     и перезапустите бота.")
    print("  3) Запустите бота на сервере (VPS) с доступом к Telegram.")
    print("=" * 60)
    return False


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан TELEGRAM_BOT_TOKEN.\n"
            "Пример (PowerShell): $env:TELEGRAM_BOT_TOKEN='123:ABC'; python telegram_bot.py"
        )
    print(f"HabitFlow-бот запущен. Трекер: {HABITFLOW_URL}")
    check_connection()

    offset = 0
    warned = False
    while True:
        result = tg("getUpdates", {"timeout": 30, "offset": offset})
        if not result.get("ok"):
            if not warned:
                print("⚠ Нет связи с Telegram:", result.get("error"),
                      "— повторяю попытки каждые 5 c. Ctrl+C для выхода.")
                warned = True
            time.sleep(5)
            continue
        warned = False
        for update in result.get("result", []):
            offset = update["update_id"] + 1
            try:
                handle_update(update)
            except Exception as e:  # бот не должен падать
                print("update error:", e)


if __name__ == "__main__":
    main()
