"""HabitFlow - трекер привычек.

Этап 1 (MVP): регистрация, CRUD привычек, отметки, стрики, напоминания.
Этап 2 (ядро): категории, теги, статистика (неделя/месяц/год), экспорт CSV.
Этап 3 (геймификация): Imperfection, бейджи, цитаты, соцшеринг, виджет.
Этап v3: AI-коуч и рекомендации, REST API v1, интеграции (iCalendar,
webhook, экспорт/импорт JSON).

Стек: Flask + SQLite (offline-first, данные хранятся локально).
"""

import os
import io
import csv
import json
import hmac
import time
import random
import secrets
import calendar
import sqlite3
import threading
import urllib.request
from datetime import datetime, date, timedelta, timezone, time as dtime
from functools import wraps

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from flask import (
    Flask, g, render_template, request, redirect, url_for,
    session, flash, jsonify, Response,
)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Путь к базе можно задать переменной HABITFLOW_DB (нужно на хостинге с
# постоянным диском, например Railway Volume: /data/habits.db).
if os.environ.get("HABITFLOW_DB"):
    DB_PATH = os.path.abspath(os.environ["HABITFLOW_DB"])
    INSTANCE_DIR = os.path.dirname(DB_PATH)
else:
    INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
    DB_PATH = os.path.join(INSTANCE_DIR, "habits.db")

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
MONTHS_SHORT = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн",
                "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
COLORS = ["#6c5ce7", "#00b894", "#e17055", "#0984e3", "#fdcb6e", "#d63031", "#00cec9", "#e84393"]
ICONS = ["✅", "💧", "📖", "🏃", "🧘", "💪", "🥗", "😴", "🧹", "🎯", "🚭", "📝"]

DEFAULT_CATEGORIES = [
    ("Здоровье", "#00b894", "💚"),
    ("Работа", "#0984e3", "💼"),
    ("Спорт", "#e17055", "🏃"),
]

# Бейджи за длину серии (код, порог в днях, иконка, название, описание).
BADGES = [
    ("streak_7", 7, "🔥", "Неделя силы", "7 дней подряд"),
    ("streak_30", 30, "💪", "Месяц дисциплины", "30 дней подряд"),
    ("streak_100", 100, "🏆", "Железная воля", "100 дней подряд"),
]
BADGE_MAP = {code: {"code": code, "days": days, "icon": icon, "title": title, "desc": desc}
             for code, days, icon, title, desc in BADGES}

# Мотивирующие цитаты при отметке.
QUOTES = [
    "Маленькие шаги каждый день приводят к большим переменам.",
    "Дисциплина — это выбор между тем, что ты хочешь сейчас, и тем, что хочешь больше всего.",
    "Ты не обязан быть великим, чтобы начать. Но нужно начать, чтобы стать великим.",
    "Привычка — это не цель, это путь. Продолжай идти.",
    "Сегодняшняя отметка — вклад в себя завтрашнего.",
    "Не идеально, а регулярно. Регулярность побеждает талант.",
    "Каждый день — ещё один кирпич в стену твоего будущего.",
    "Ты сильнее, чем думаешь. Один день за раз.",
    "Прогресс важнее совершенства.",
    "Пропустишь день — не страшно. Пропустишь два — начнётся новая привычка.",
]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "habitflow-dev-secret-change-me")


# --------------------------------------------------------------------------
# База данных
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    email                 TEXT NOT NULL UNIQUE,
    password_hash         TEXT NOT NULL,
    reminder_time         TEXT NOT NULL DEFAULT '20:00',
    notifications_enabled INTEGER NOT NULL DEFAULT 1,
    timezone              TEXT NOT NULL DEFAULT 'Europe/Moscow',
    api_key               TEXT,
    webhook_url           TEXT,
    telegram_chat_id      TEXT,
    telegram_last_sent    TEXT,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS habits (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    note        TEXT,
    icon        TEXT NOT NULL DEFAULT '✅',
    color       TEXT NOT NULL DEFAULT '#6c5ce7',
    days        TEXT NOT NULL DEFAULT '0,1,2,3,4,5,6',  -- Пн..Вс
    remind_time   TEXT,                                 -- 'HH:MM' время действия (или NULL)
    remind_before INTEGER NOT NULL DEFAULT 10,          -- за сколько минут напомнить
    start_date    TEXT,                                 -- YYYY-MM-DD, не раньше этой даты
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    imperfection INTEGER NOT NULL DEFAULT 0,  -- режим «Imperfection»: один пропуск прощается
    archived    INTEGER NOT NULL DEFAULT 0,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT '#6c5ce7',
    icon       TEXT NOT NULL DEFAULT '🏷',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS habit_tags (
    habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (habit_id, tag_id)
);

CREATE TABLE IF NOT EXISTS badge_awards (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    habit_id  INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    code      TEXT NOT NULL,
    earned_at TEXT NOT NULL,
    UNIQUE (habit_id, code)
);

CREATE TABLE IF NOT EXISTS habit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id   INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    date       TEXT NOT NULL,          -- YYYY-MM-DD
    done       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (habit_id, date)
);

CREATE INDEX IF NOT EXISTS idx_logs_habit ON habit_logs(habit_id, date);
CREATE INDEX IF NOT EXISTS idx_habits_user ON habits(user_id);
CREATE INDEX IF NOT EXISTS idx_categories_user ON categories(user_id);
CREATE INDEX IF NOT EXISTS idx_tags_user ON tags(user_id);

-- Журнал отправленных напоминаний: защита от дублей (habit_id, date, slot).
CREATE TABLE IF NOT EXISTS reminder_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    date     TEXT NOT NULL,          -- локальная дата пользователя YYYY-MM-DD
    slot     TEXT NOT NULL,          -- 'pre' | 'on' | 'r1' | 'r2' | 'r3'
    sent_at  TEXT NOT NULL,
    UNIQUE (habit_id, date, slot)
);

-- Отложенные напоминания (кнопка «Отложить»), send_at в UTC.
CREATE TABLE IF NOT EXISTS reminder_snooze (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id   INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    send_at    TEXT NOT NULL,
    sent       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        os.makedirs(INSTANCE_DIR, exist_ok=True)
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
    return db


@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    # Миграция для баз, созданных на Этапе 1 (без категорий и тегов).
    cols = {row[1] for row in con.execute("PRAGMA table_info(habits)")}
    if "category_id" not in cols:
        con.execute(
            "ALTER TABLE habits ADD COLUMN category_id INTEGER "
            "REFERENCES categories(id) ON DELETE SET NULL"
        )
    if "imperfection" not in cols:
        con.execute("ALTER TABLE habits ADD COLUMN imperfection INTEGER NOT NULL DEFAULT 0")
    # Миграция: время действия, напоминание за N минут, дата начала.
    if "remind_time" not in cols:
        con.execute("ALTER TABLE habits ADD COLUMN remind_time TEXT")
    if "remind_before" not in cols:
        con.execute("ALTER TABLE habits ADD COLUMN remind_before INTEGER NOT NULL DEFAULT 10")
    if "start_date" not in cols:
        con.execute("ALTER TABLE habits ADD COLUMN start_date TEXT")
    # Миграция для users (API-ключ и webhook), Этап v3.
    ucols = {row[1] for row in con.execute("PRAGMA table_info(users)")}
    if "api_key" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN api_key TEXT")
    if "webhook_url" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN webhook_url TEXT")
    if "telegram_chat_id" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN telegram_chat_id TEXT")
    if "telegram_last_sent" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN telegram_last_sent TEXT")
    if "timezone" not in ucols:
        con.execute("ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Europe/Moscow'")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key)")
    con.commit()
    con.close()


def seed_default_categories(user_id):
    db = get_db()
    if db.execute("SELECT 1 FROM categories WHERE user_id = ?", (user_id,)).fetchone():
        return
    now = datetime.now().isoformat()
    for name, color, icon in DEFAULT_CATEGORIES:
        db.execute(
            "INSERT INTO categories (user_id, name, color, icon, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, color, icon, now),
        )
    db.commit()


# --------------------------------------------------------------------------
# Домен: расписание и стрики
# --------------------------------------------------------------------------

def parse_days(raw):
    """'0,1,2' -> {0,1,2}; пусто/None -> все дни."""
    if not raw:
        return set(range(7))
    try:
        return {int(x) for x in str(raw).split(",") if x.strip() != ""}
    except ValueError:
        return set(range(7))


def scheduled_on(days, d):
    return d.weekday() in parse_days(days)


def streak_metrics(done_dates, days, imperfection, today):
    """Текущая и лучшая серия с учётом расписания и режима Imperfection.

    Imperfection прощает один пропущенный запланированный день внутри серии.
    """
    done = set(done_dates)
    if not done:
        return 0, 0
    sched = parse_days(days)
    start = min(done)
    run = 0
    best = 0
    grace = 1 if imperfection else 0
    for d in date_range(start, today):
        if d.weekday() not in sched:
            continue
        if d in done:
            run += 1
            best = max(best, run)
        elif d == today:
            continue  # сегодня ещё можно успеть — серию не обрываем
        elif grace > 0:
            grace -= 1  # прощаем один пропуск
        else:
            run = 0
            grace = 1 if imperfection else 0
    return run, best


def current_streak(done_dates, today, days=None, imperfection=False):
    return streak_metrics(done_dates, days, imperfection, today)[0]


def best_streak(done_dates, days=None, imperfection=False, today=None):
    return streak_metrics(done_dates, days, imperfection, today or date.today())[1]


def habit_done_dates(habit_id):
    rows = get_db().execute(
        "SELECT date FROM habit_logs WHERE habit_id = ? AND done = 1",
        (habit_id,),
    ).fetchall()
    return [datetime.strptime(r["date"], "%Y-%m-%d").date() for r in rows]


def habit_stats(habit):
    done = habit_done_dates(habit["id"])
    today = date.today()
    streak, best = streak_metrics(
        done, habit["days"], bool(habit["imperfection"]), today
    )
    return {
        "streak": streak,
        "best": best,
        "total": len(done),
        "done_set": set(done),
        "done_today": today in set(done),
        "imperfection": bool(habit["imperfection"]),
    }


# --------------------------------------------------------------------------
# Домен: геймификация (бейджи, цитаты, защита серии)
# --------------------------------------------------------------------------

def earned_badges(habit_id):
    rows = get_db().execute(
        "SELECT code FROM badge_awards WHERE habit_id = ?", (habit_id,)
    ).fetchall()
    return {r["code"] for r in rows}


def habit_badges(habit_id):
    """Все бейджи привычки с флагом earned (для отображения)."""
    earned = earned_badges(habit_id)
    return [dict(b, earned=b["code"] in earned) for b in BADGE_MAP.values()]


def award_badges(user_id, habit_id, best):
    """Выдаёт бейджи, достигнутые по длине серии. Возвращает список новых."""
    earned = earned_badges(habit_id)
    new = []
    now = datetime.now().isoformat()
    db = get_db()
    for code, days, icon, title, desc in BADGES:
        if best >= days and code not in earned:
            db.execute(
                "INSERT OR IGNORE INTO badge_awards (user_id, habit_id, code, earned_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, habit_id, code, now),
            )
            new.append({"code": code, "icon": icon, "title": title, "desc": desc})
    if new:
        db.commit()
    return new


def random_quote():
    return random.choice(QUOTES)


# --------------------------------------------------------------------------
# Домен: AI-коуч (рекомендации) и API-ключи
# --------------------------------------------------------------------------

WEEKDAY_FULL = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]


def ensure_api_key(user_id):
    """Возвращает существующий API-ключ пользователя или создаёт новый."""
    db = get_db()
    row = db.execute("SELECT api_key FROM users WHERE id = ?", (user_id,)).fetchone()
    if row and row["api_key"]:
        return row["api_key"]
    key = "hf_" + secrets.token_urlsafe(24)
    db.execute("UPDATE users SET api_key = ? WHERE id = ?", (key, user_id))
    db.commit()
    return key


def regenerate_api_key(user_id):
    db = get_db()
    key = "hf_" + secrets.token_urlsafe(24)
    db.execute("UPDATE users SET api_key = ? WHERE id = ?", (key, user_id))
    db.commit()
    return key


def user_by_api_key(key):
    if not key:
        return None
    return get_db().execute("SELECT * FROM users WHERE api_key = ?", (key,)).fetchone()


def coach_insights(user_id):
    """Эвристический «AI-коуч»: анализирует данные и формирует рекомендации.

    Возвращает {summary, focus, insights[]}. Каждый инсайт:
    {kind, icon, title, text}, где kind ∈ praise|warning|tip|motivation.
    """
    db = get_db()
    habits = db.execute(
        "SELECT * FROM habits WHERE user_id = ? AND archived = 0 ORDER BY sort_order, id",
        (user_id,),
    ).fetchall()

    if not habits:
        return {
            "summary": "Создайте первую привычку — и коуч начнёт давать рекомендации.",
            "focus": None,
            "insights": [{
                "kind": "tip", "icon": "🌱", "title": "Начните с малого",
                "text": "Добавьте 1–2 простые привычки (вода, прогулка). Коуч проанализирует "
                        "ваши отметки и подскажет, что улучшить.",
            }],
        }

    today = date.today()
    habit_ids = [h["id"] for h in habits]
    placeholders = ",".join("?" * len(habit_ids))
    log_rows = db.execute(
        f"SELECT habit_id, date, done FROM habit_logs WHERE habit_id IN ({placeholders})",
        tuple(habit_ids),
    ).fetchall()
    done_set = {(r["habit_id"], r["date"]) for r in log_rows if r["done"]}

    # Метрики за последние 30 дней.
    window_start = today - timedelta(days=29)
    stats = []
    weekday_done = {i: [0, 0] for i in range(7)}  # [done, expected]
    for h in habits:
        expected = done = 0
        st = habit_stats(h)
        for d in date_range(window_start, today):
            if not scheduled_on(h["days"], d):
                continue
            expected += 1
            is_done = (h["id"], d.isoformat()) in done_set
            if is_done:
                done += 1
            weekday_done[d.weekday()][1] += 1
            if is_done:
                weekday_done[d.weekday()][0] += 1
        pct = round(done / expected * 100) if expected else 0
        stats.append({"habit": h, "done": done, "expected": expected, "pct": pct,
                      "streak": st["streak"], "best": st["best"], "imperfection": st["imperfection"]})

    total_done = sum(s["done"] for s in stats)
    total_expected = sum(s["expected"] for s in stats)
    overall = round(total_done / total_expected * 100) if total_expected else 0

    insights = []

    # 1) Празднование рекордной серии.
    top = max(stats, key=lambda s: s["best"])
    if top["best"] >= 3:
        insights.append({
            "kind": "praise", "icon": "🏆",
            "title": f"Рекорд по «{top['habit']['title']}» — {top['best']} дн.",
            "text": "Отличная серия! Импульс на вашей стороне — не прерывайте цепочку.",
        })

    # 2) Слабое звено — самая низкая дисциплина.
    weakest = min(stats, key=lambda s: s["pct"])
    if weakest["expected"] >= 3 and weakest["pct"] < 60:
        insights.append({
            "kind": "warning", "icon": "⚠️",
            "title": f"«{weakest['habit']['title']}» выполняется лишь на {weakest['pct']}%",
            "text": "Слишком сложно? Уменьшите цель или число дней в неделю, "
                    "чтобы вернуть регулярность. Начните с 2–3 дней.",
        })

    # 3) Самый стабильный день недели / провал по дням.
    valid_days = [(i, v) for i, v in weekday_done.items() if v[1] >= 2]
    if valid_days:
        best_day = max(valid_days, key=lambda kv: kv[1][0] / kv[1][1])
        worst_day = min(valid_days, key=lambda kv: kv[1][0] / kv[1][1])
        br = round(best_day[1][0] / best_day[1][1] * 100)
        wr = round(worst_day[1][0] / worst_day[1][1] * 100)
        insights.append({
            "kind": "tip", "icon": "📅",
            "title": f"Лучший день — {WEEKDAY_FULL[best_day[0]]} ({br}%), слабый — {WEEKDAY_FULL[worst_day[0]]} ({wr}%)",
            "text": "Планируйте сложные привычки на «сильные» дни и ставьте напоминание "
                    "на «слабые», чтобы не терять темп.",
        })

    # 4) Приближение к бейджу.
    for s in stats:
        for code, days, icon, title, desc in BADGES:
            if s["best"] < days and days - s["best"] <= 3 and s["streak"] >= days - 3:
                insights.append({
                    "kind": "motivation", "icon": icon,
                    "title": f"{desc} до награды «{title}» по «{s['habit']['title']}»",
                    "text": f"Осталось {days - s['streak']} — вы почти у цели!",
                })
                break

    # 5) Напоминания не настроены.
    user = db.execute("SELECT reminder_time, notifications_enabled FROM users WHERE id = ?",
                      (user_id,)).fetchone()
    if user and not user["notifications_enabled"]:
        insights.append({
            "kind": "tip", "icon": "🔔", "title": "Включите напоминания",
            "text": "Уведомления повышают стабильность отметок. Установите время в Настройках.",
        })

    # 6) Перегрузка.
    if len(habits) > 8 and overall < 55:
        insights.append({
            "kind": "warning", "icon": "🧯",
            "title": f"{len(habits)} привычек, но выполнение {overall}%",
            "text": "Похоже на перегрузку. Оставьте 3–5 ключевых привычек на этот месяц — "
                    "качество важнее количества.",
        })

    # 7) Универсальный совет по расписанию на сегодня.
    scheduled_today = [s for s in stats if scheduled_on(s["habit"]["days"], today)]
    if scheduled_today:
        pending = [s for s in scheduled_today if (s["habit"]["id"], today.isoformat()) not in done_set]
        if pending:
            names = ", ".join(s["habit"]["title"] for s in pending[:3])
            insights.append({
                "kind": "motivation", "icon": "🎯",
                "title": f"На сегодня осталось: {len(pending)}",
                "text": f"Начните с «{names}». Первый шаг — самый важный.",
            })

    # 8) Похвала при высокой общей дисциплине.
    if overall >= 80:
        insights.append({
            "kind": "praise", "icon": "🌟", "title": f"Дисциплина {overall}% за 30 дней",
            "text": "Вы в верхней когорте. Попробуйте добавить одну новую привычку.",
        })

    if not insights:
        insights.append({
            "kind": "tip", "icon": "💡", "title": "Данных пока мало",
            "text": "Отмечайте привычки несколько дней подряд — коуч проанализирует "
                    "динамику и даст точные рекомендации.",
        })

    # Приоритет: приоритетные рекомендации выше.
    focus = None
    for kind in ("warning", "tip", "motivation", "praise"):
        for ins in insights:
            if ins["kind"] == kind:
                focus = ins
                break
        if focus:
            break

    if overall >= 75:
        summary = f"Отличный темп — {overall}% выполнения за 30 дней."
    elif overall >= 45:
        summary = f"Хороший прогресс — {overall}% за 30 дней. Есть куда расти."
    else:
        summary = f"Текущий уровень — {overall}% за 30 дней. Сфокусируемся на лучших днях."

    # Необязательное обогащение через LLM (если задан HABITFLOW_LLM_URL).
    context = (
        f"Привычки: {', '.join(s['habit']['title'] for s in stats)}. "
        f"Общее выполнение {overall}% за 30 дней. "
        f"Серии: " + "; ".join(f"{s['habit']['title']}={s['streak']}" for s in stats) + "."
    )
    note = llm_coach_note(context)
    if note:
        insights.insert(0, note)

    return {"summary": summary, "focus": focus, "insights": insights}


def dispatch_webhook(user, payload):
    """Best-effort отправка события на webhook пользователя (интеграция)."""
    url = user["webhook_url"] if user and "webhook_url" in user.keys() else None
    if not url:
        return False
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "HabitFlow/1.0"},
        )
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


def llm_coach_note(context):
    """Необязательный генеративный совет через LLM (HABITFLOW_LLM_URL).

    По умолчанию выключен — коуч полностью работает офлайн на эвристиках.
    """
    url = os.environ.get("HABITFLOW_LLM_URL")
    if not url:
        return None
    try:
        headers = {"Content-Type": "application/json", "User-Agent": "HabitFlow/1.0"}
        key = os.environ.get("HABITFLOW_LLM_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = json.dumps({"input": context, "max_tokens": 160}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data.get("output") or data.get("text") or data.get("response") or ""
        text = (text if isinstance(text, str) else "").strip()
        if text:
            return {"kind": "tip", "icon": "✨", "title": "Совет AI-коуча", "text": text}
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# Telegram: push-напоминания (серверная рассылка)
# --------------------------------------------------------------------------

def telegram_configured():
    """True, если задан токен бота — иначе push-напоминания недоступны."""
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


def tg_send_message(chat_id, text, reply_markup=None):
    """Best-effort отправка сообщения через Telegram Bot API."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        return False
    payload = {"chat_id": chat_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            data=data, method="POST", headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


DEFAULT_TZ = "Europe/Moscow"
COMMON_TIMEZONES = [
    "Europe/Kaliningrad", "Europe/Moscow", "Europe/Samara", "Asia/Yekaterinburg",
    "Asia/Omsk", "Asia/Krasnoyarsk", "Asia/Novosibirsk", "Asia/Irkutsk",
    "Asia/Yakutsk", "Asia/Vladivostok", "Asia/Magadan", "Asia/Kamchatka",
    "Europe/Kyiv", "Europe/Minsk", "Asia/Almaty", "Asia/Tbilisi", "Asia/Yerevan",
    "Asia/Tashkent", "Asia/Dubai", "Europe/London", "Europe/Berlin", "UTC",
]
REMINDER_WINDOW = 10  # минут: в каком окне после момента считаем напоминание наступившим
REPEAT_EVERY = 30     # минут между повторами, если не отмечено
REPEAT_MAX = 3        # максимум повторов


def user_tz(user):
    """Часовой пояс пользователя (ZoneInfo) с надёжным запасным вариантом."""
    name = None
    try:
        name = user["timezone"]
    except (KeyError, IndexError):
        name = None
    if ZoneInfo is not None:
        for cand in (name, DEFAULT_TZ):
            if not cand:
                continue
            try:
                return ZoneInfo(cand)
            except Exception:
                continue
    return timezone.utc


def local_date(user, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    return now_utc.astimezone(user_tz(user)).date()


def habit_reminder_keyboard(habit_id):
    """Inline-кнопки под напоминанием: «Сделал», «Отложить», «Пропустить»."""
    return {"inline_keyboard": [
        [{"text": "✅ Сделал", "callback_data": "t:" + str(habit_id)},
         {"text": "⏰ +10 мин", "callback_data": "s:" + str(habit_id)}],
        [{"text": "❌ Пропустить сегодня", "callback_data": "x:" + str(habit_id)}],
    ]}


def habit_reminder_text(habit, slot, lead, when):
    icon = habit["icon"] or "✅"
    title = habit["title"]
    hhmm = when.strftime("%H:%M")
    if slot == "pre":
        return (f"🔔 <b>{icon} {title}</b>\n"
                f"Старт через {lead} мин (в {hhmm}). Готовься.")
    if slot == "on":
        return (f"⏰ <b>{icon} {title}</b>\n"
                f"Пора действовать — {hhmm}. Как успехи?")
    tip = random.choice(QUOTES)
    return (f"😤 <b>{icon} {title}</b> — пока не отмечено.\n"
            f"Держим курс на цель: {tip}")


def _slot_sent(db, habit_id, day, slot):
    return db.execute(
        "SELECT 1 FROM reminder_log WHERE habit_id=? AND date=? AND slot=?",
        (habit_id, day, slot),
    ).fetchone() is not None


def _log_slot(db, habit_id, day, slot):
    db.execute(
        "INSERT OR IGNORE INTO reminder_log (habit_id, date, slot, sent_at) VALUES (?, ?, ?, ?)",
        (habit_id, day, slot, datetime.now(timezone.utc).isoformat()),
    )


def _habit_done(db, habit_id, day):
    return db.execute(
        "SELECT 1 FROM habit_logs WHERE habit_id=? AND date=? AND done=1",
        (habit_id, day),
    ).fetchone() is not None


def _dispatch_snoozes(db, u, now_utc, now_local):
    """Отправляет отложенные напоминания (кнопка «⏰ +10 мин»)."""
    rows = db.execute(
        "SELECT * FROM reminder_snooze WHERE user_id=? AND sent=0 AND send_at <= ?",
        (u["id"], now_utc.isoformat()),
    ).fetchall()
    sent = 0
    for s in rows:
        db.execute("UPDATE reminder_snooze SET sent=1 WHERE id=?", (s["id"],))
        if _habit_done(db, s["habit_id"], now_local.date().isoformat()):
            continue
        h = db.execute("SELECT * FROM habits WHERE id=?", (s["habit_id"],)).fetchone()
        if not h:
            continue
        text = (f"⏰ <b>{h['icon']} {h['title']}</b>\n"
                "Отложенное напоминание — не забудь отметиться!")
        if tg_send_message(u["telegram_chat_id"], text, habit_reminder_keyboard(h["id"])):
            sent += 1
    return sent


def _dispatch_slots_for_user(db, u, now_local):
    today = now_local.date()
    day = today.isoformat()
    weekday = today.weekday()
    habits = db.execute(
        "SELECT * FROM habits WHERE user_id = ? AND archived = 0 ORDER BY sort_order, id",
        (u["id"],),
    ).fetchall()
    sent = 0
    for h in habits:
        remind = (h["remind_time"] or "").strip()
        if not remind:
            continue
        if weekday not in parse_days(h["days"]):
            continue
        if h["start_date"] and h["start_date"] > day:
            continue
        try:
            hh, mm = (int(x) for x in remind.split(":")[:2])
            base = datetime.combine(today, dtime(hh, mm), tzinfo=now_local.tzinfo)
        except (ValueError, TypeError):
            continue
        lead = h["remind_before"]
        lead = 10 if lead is None else int(lead)
        slots = []
        if lead > 0:
            slots.append(("pre", base - timedelta(minutes=lead)))
        slots.append(("on", base))
        for i in range(1, REPEAT_MAX + 1):
            slots.append((f"r{i}", base + timedelta(minutes=REPEAT_EVERY * i)))
        if _habit_done(db, h["id"], day):
            continue
        for slot, when in slots:
            if not (when <= now_local < when + timedelta(minutes=REMINDER_WINDOW)):
                continue
            if _slot_sent(db, h["id"], day, slot):
                continue
            _log_slot(db, h["id"], day, slot)
            if tg_send_message(u["telegram_chat_id"],
                               habit_reminder_text(h, slot, lead, when),
                               habit_reminder_keyboard(h["id"])):
                sent += 1
    return sent


def dispatch_telegram_reminders(now=None):
    """Рассылает напоминания по привычкам с учётом часового пояса. Возвращает число отправок."""
    db = get_db()
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    rows = db.execute(
        "SELECT * FROM users WHERE telegram_chat_id IS NOT NULL AND notifications_enabled = 1"
    ).fetchall()
    sent = 0
    for u in rows:
        now_local = now_utc.astimezone(user_tz(u))
        sent += _dispatch_snoozes(db, u, now_utc, now_local)
        sent += _dispatch_slots_for_user(db, u, now_local)
    db.commit()
    return sent


def start_reminder_scheduler(interval=60):
    """Запускает фоновый поток, который раз в минуту проверяет напоминания."""
    def loop():
        while True:
            try:
                with app.app_context():
                    dispatch_telegram_reminders()
            except Exception as e:  # поток не должен падать
                print("reminder scheduler error:", e)
            time.sleep(interval)

    thread = threading.Thread(target=loop, daemon=True, name="habitflow-reminders")
    thread.start()
    return thread


# --------------------------------------------------------------------------
# Домен: категории, теги, аналитика
# --------------------------------------------------------------------------

def user_categories(user_id):
    return get_db().execute(
        "SELECT * FROM categories WHERE user_id = ? ORDER BY name",
        (user_id,),
    ).fetchall()


def habit_tags_map(habit_ids):
    """{habit_id: [тег, ...]} для набора привычек."""
    if not habit_ids:
        return {}
    placeholders = ",".join("?" * len(habit_ids))
    rows = get_db().execute(
        "SELECT ht.habit_id AS hid, t.name AS name "
        "FROM habit_tags ht JOIN tags t ON t.id = ht.tag_id "
        f"WHERE ht.habit_id IN ({placeholders}) ORDER BY t.name",
        tuple(habit_ids),
    ).fetchall()
    result = {}
    for row in rows:
        result.setdefault(row["hid"], []).append(row["name"])
    return result


def save_habit_tags(habit_id, raw, user_id):
    """Синхронизирует теги привычки из строки 'здоровье, утро, спорт'."""
    db = get_db()
    db.execute("DELETE FROM habit_tags WHERE habit_id = ?", (habit_id,))
    names = []
    seen = set()
    for part in (raw or "").split(","):
        name = part.strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    for name in names[:15]:
        row = db.execute(
            "SELECT id FROM tags WHERE user_id = ? AND name = ?", (user_id, name)
        ).fetchone()
        tag_id = row["id"] if row else db.execute(
            "INSERT INTO tags (user_id, name) VALUES (?, ?)", (user_id, name)
        ).lastrowid
        db.execute(
            "INSERT OR IGNORE INTO habit_tags (habit_id, tag_id) VALUES (?, ?)",
            (habit_id, tag_id),
        )


def date_range(start, end):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def build_period(period, today):
    """Возвращает список бакетов {label, start, end} для недели/месяца/года."""
    period = period if period in ("week", "month", "year") else "week"
    buckets = []
    if period == "year":
        for m in range(1, 13):
            first = date(today.year, m, 1)
            last = date(today.year, m, calendar.monthrange(today.year, m)[1])
            buckets.append({"label": MONTHS_SHORT[m - 1], "start": first, "end": last})
    elif period == "month":
        first = date(today.year, today.month, 1)
        last = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        for d in date_range(first, last):
            buckets.append({"label": str(d.day), "start": d, "end": d})
    else:
        monday = today - timedelta(days=today.weekday())
        for i in range(7):
            d = monday + timedelta(days=i)
            buckets.append({"label": WEEKDAYS[i], "start": d, "end": d})
    return period, buckets


def period_report(habits, period, today):
    """Расчёт выполнения по бакетам, по привычкам и по категориям."""
    _, buckets = build_period(period, today)

    habits = list(habits)
    habit_ids = [h["id"] for h in habits]
    done_pairs = set()
    if habit_ids:
        placeholders = ",".join("?" * len(habit_ids))
        rows = get_db().execute(
            f"SELECT habit_id, date FROM habit_logs WHERE done = 1 "
            f"AND habit_id IN ({placeholders})",
            tuple(habit_ids),
        ).fetchall()
        done_pairs = {(r["habit_id"], r["date"]) for r in rows}

    # Бакеты: сколько выполнено и сколько было запланировано.
    for bucket in buckets:
        bucket["done"] = 0
        bucket["expected"] = 0
        for h in habits:
            for d in date_range(bucket["start"], bucket["end"]):
                if d > today:
                    continue
                if not scheduled_on(h["days"], d):
                    continue
                bucket["expected"] += 1
                if (h["id"], d.isoformat()) in done_pairs:
                    bucket["done"] += 1

    # По привычкам (за весь период, до сегодня).
    start = min(b["start"] for b in buckets)
    end = min(max(b["end"] for b in buckets), today)
    per_habit = []
    for h in habits:
        expected = done = 0
        for d in date_range(start, end):
            if scheduled_on(h["days"], d):
                expected += 1
                if (h["id"], d.isoformat()) in done_pairs:
                    done += 1
        pct = round(done / expected * 100) if expected else 0
        per_habit.append({"habit": h, "done": done, "expected": expected, "pct": pct})
    per_habit.sort(key=lambda x: x["pct"])

    total_done = sum(b["done"] for b in buckets)
    total_expected = sum(b["expected"] for b in buckets)
    overall = round(total_done / total_expected * 100) if total_expected else 0
    miss = max(total_expected - total_done, 0)
    max_bucket = max([b["expected"] for b in buckets] + [1])

    for bucket in buckets:
        bucket["pct"] = round(bucket["done"] / bucket["expected"] * 100) if bucket["expected"] else 0
        bucket["height"] = round(bucket["done"] / max_bucket * 100) if max_bucket else 0

    return {
        "period": period,
        "buckets": buckets,
        "per_habit": per_habit,
        "total_done": total_done,
        "total_expected": total_expected,
        "miss": miss,
        "overall": overall,
        "done_pct": round(total_done / max(total_done + miss, 1) * 100),
        "miss_pct": round(miss / max(total_done + miss, 1) * 100),
        "start": start,
        "end": end,
    }


def category_report(habits, done_pairs, user_id):
    """Агрегация выполнения по категориям (за всё время до сегодня)."""
    cats = {c["id"]: c for c in user_categories(user_id)}
    today = date.today()
    agg = {}
    for h in habits:
        key = h["category_id"] if h["category_id"] in cats else 0
        entry = agg.setdefault(key, {"done": 0, "expected": 0})
        created = None
        try:
            created = datetime.fromisoformat(h["created_at"]).date()
        except (ValueError, TypeError):
            created = today
        for d in date_range(created, today):
            if scheduled_on(h["days"], d):
                entry["expected"] += 1
                if (h["id"], d.isoformat()) in done_pairs:
                    entry["done"] += 1
    result = []
    for key, entry in agg.items():
        cat = cats.get(key)
        pct = round(entry["done"] / entry["expected"] * 100) if entry["expected"] else 0
        result.append({
            "name": cat["name"] if cat else "Без категории",
            "color": cat["color"] if cat else "#9aa0b5",
            "icon": cat["icon"] if cat else "🏷",
            "done": entry["done"],
            "expected": entry["expected"],
            "pct": pct,
        })
    result.sort(key=lambda x: x["pct"], reverse=True)
    return result


# --------------------------------------------------------------------------
# Аутентификация
# --------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id") or current_user() is None:
            session.clear()
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def api_auth(view):
    """Авторизация REST API по ключу: заголовок X-API-Key или ?key=."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        key = request.headers.get("X-API-Key") or request.args.get("key")
        user = user_by_api_key(key)
        if not user:
            return jsonify({"error": "unauthorized", "hint": "Укажите X-API-Key"}), 401
        g.api_user = user
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_globals():
    return {
        "user": current_user() if session.get("user_id") else None,
        "WEEKDAYS": WEEKDAYS,
        "COLORS": COLORS,
        "ICONS": ICONS,
        "today": date.today(),
    }


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not email or "@" not in email:
            flash("Введите корректный email", "error")
        elif len(password) < 6:
            flash("Пароль должен содержать минимум 6 символов", "error")
        else:
            db = get_db()
            exists = db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
            if exists:
                flash("Такой email уже зарегистрирован", "error")
            else:
                cur = db.execute(
                    "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                    (email, generate_password_hash(password), datetime.now().isoformat()),
                )
                db.commit()
                session.clear()
                session["user_id"] = cur.lastrowid
                seed_default_categories(cur.lastrowid)
                flash("Аккаунт создан. Добро пожаловать!", "success")
                return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        row = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session["user_id"] = row["id"]
            return redirect(url_for("index"))
        flash("Неверный email или пароль", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Привычки
# --------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    db = get_db()
    uid = session["user_id"]
    today = date.today()

    habits = db.execute(
        "SELECT * FROM habits WHERE user_id = ? AND archived = 0 ORDER BY sort_order, id",
        (uid,),
    ).fetchall()

    cats = {c["id"]: c for c in user_categories(uid)}
    tags_map = habit_tags_map([h["id"] for h in habits])

    items = []
    for h in habits:
        items.append({
            "habit": h,
            "stats": habit_stats(h),
            "scheduled": scheduled_on(h["days"], today),
            "category": cats.get(h["category_id"]),
            "tags": tags_map.get(h["id"], []),
        })

    scheduled_items = [i for i in items if i["scheduled"]]
    done_count = sum(1 for i in scheduled_items if i["stats"]["done_today"])
    progress = round(done_count / len(scheduled_items) * 100) if scheduled_items else 0

    return render_template(
        "index.html",
        items=items,
        scheduled_items=scheduled_items,
        categories=list(cats.values()),
        done_count=done_count,
        progress=progress,
        quote=random_quote(),
    )


def _clean_time(raw):
    """'8:5' -> '08:05'; пусто/мусор -> None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        hh, mm = (int(x) for x in raw.split(":")[:2])
        if 0 <= hh < 24 and 0 <= mm < 60:
            return f"{hh:02d}:{mm:02d}"
    except (ValueError, TypeError):
        pass
    return None


def _clean_date(raw):
    """'YYYY-MM-DD' -> строка; пусто/мусор -> None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        datetime.strptime(raw, "%Y-%m-%d")
        return raw
    except ValueError:
        return None


def _read_habit_form():
    title = (request.form.get("title") or "").strip()
    days = request.form.getlist("days")
    category_id = request.form.get("category_id", type=int)
    remind_before = request.form.get("remind_before", type=int)
    if remind_before is None or remind_before < 0 or remind_before > 1440:
        remind_before = 10
    return {
        "title": title,
        "note": (request.form.get("note") or "").strip(),
        "icon": request.form.get("icon") or "✅",
        "color": request.form.get("color") or "#6c5ce7",
        "days": ",".join(sorted(days, key=int)) if days else "0,1,2,3,4,5,6",
        "category_id": category_id,
        "tags": (request.form.get("tags") or "").strip(),
        "imperfection": 1 if request.form.get("imperfection") else 0,
        "remind_time": _clean_time(request.form.get("remind_time")),
        "remind_before": remind_before,
        "start_date": _clean_date(request.form.get("start_date")),
    }


@app.route("/habits/new", methods=["GET", "POST"])
@login_required
def habit_new():
    if request.method == "POST":
        data = _read_habit_form()
        if not data["title"]:
            flash("Введите название привычки", "error")
        else:
            now = datetime.now().isoformat()
            db = get_db()
            cur = db.execute(
                """INSERT INTO habits (user_id, title, note, icon, color, days, category_id, imperfection,
                                       remind_time, remind_before, start_date, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session["user_id"], data["title"], data["note"], data["icon"],
                 data["color"], data["days"], data["category_id"],
                 data["imperfection"], data["remind_time"], data["remind_before"],
                 data["start_date"], now, now),
            )
            save_habit_tags(cur.lastrowid, data["tags"], session["user_id"])
            db.commit()
            flash("Привычка создана", "success")
            return redirect(url_for("habit_detail", habit_id=cur.lastrowid))
    return render_template(
        "habit_form.html", habit=None, selected_days=set(range(7)),
        categories=user_categories(session["user_id"]), habit_tags=[],
    )


@app.route("/habits/<int:habit_id>/edit", methods=["GET", "POST"])
@login_required
def habit_edit(habit_id):
    db = get_db()
    habit = db.execute(
        "SELECT * FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, session["user_id"]),
    ).fetchone()
    if not habit:
        flash("Привычка не найдена", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        data = _read_habit_form()
        if not data["title"]:
            flash("Введите название привычки", "error")
        else:
            db.execute(
                """UPDATE habits SET title=?, note=?, icon=?, color=?, days=?, category_id=?, imperfection=?,
                       remind_time=?, remind_before=?, start_date=?, updated_at=?
                   WHERE id=? AND user_id=?""",
                (data["title"], data["note"], data["icon"], data["color"],
                 data["days"], data["category_id"], data["imperfection"],
                 data["remind_time"], data["remind_before"], data["start_date"],
                 datetime.now().isoformat(), habit_id, session["user_id"]),
            )
            save_habit_tags(habit_id, data["tags"], session["user_id"])
            db.commit()
            flash("Изменения сохранены", "success")
            return redirect(url_for("habit_detail", habit_id=habit_id))
    habit_tags = habit_tags_map([habit_id]).get(habit_id, [])
    return render_template(
        "habit_form.html", habit=habit, selected_days=parse_days(habit["days"]),
        categories=user_categories(session["user_id"]), habit_tags=habit_tags,
    )


@app.route("/habits/<int:habit_id>/delete", methods=["POST"])
@login_required
def habit_delete(habit_id):
    db = get_db()
    db.execute(
        "DELETE FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, session["user_id"]),
    )
    db.commit()
    flash("Привычка удалена", "success")
    return redirect(url_for("index"))


@app.route("/habits/<int:habit_id>")
@login_required
def habit_detail(habit_id):
    db = get_db()
    habit = db.execute(
        "SELECT * FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, session["user_id"]),
    ).fetchone()
    if not habit:
        flash("Привычка не найдена", "error")
        return redirect(url_for("index"))

    stats = habit_stats(habit)

    year = request.args.get("year", type=int) or date.today().year
    month = request.args.get("month", type=int) or date.today().month
    if month < 1:
        year, month = year - 1, 12
    elif month > 12:
        year, month = year + 1, 1

    cal = calendar.Calendar(firstweekday=0)
    weeks = cal.monthdayscalendar(year, month)

    prev_month = (month - 1) or 12
    prev_year = year - (1 if month == 1 else 0)
    next_month = 1 if month == 12 else month + 1
    next_year = year + (1 if month == 12 else 0)

    category = None
    if habit["category_id"]:
        category = get_db().execute(
            "SELECT * FROM categories WHERE id = ?", (habit["category_id"],)
        ).fetchone()

    return render_template(
        "habit_detail.html",
        habit=habit,
        stats=stats,
        weeks=weeks,
        year=year, month=month, month_name=MONTHS[month - 1],
        prev_month=prev_month, prev_year=prev_year,
        next_month=next_month, next_year=next_year,
        selected_days=parse_days(habit["days"]),
        category=category,
        habit_tags=habit_tags_map([habit_id]).get(habit_id, []),
        badges=habit_badges(habit_id),
    )


# --------------------------------------------------------------------------
# API: отметки и напоминания
# --------------------------------------------------------------------------

@app.route("/api/habits/<int:habit_id>/toggle", methods=["POST"])
@login_required
def api_toggle(habit_id):
    db = get_db()
    habit = db.execute(
        "SELECT * FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, session["user_id"]),
    ).fetchone()
    if not habit:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json(silent=True) or {}
    date_str = payload.get("date") or date.today().isoformat()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "bad date"}), 400

    row = db.execute(
        "SELECT * FROM habit_logs WHERE habit_id = ? AND date = ?",
        (habit_id, date_str),
    ).fetchone()

    if row and row["done"]:
        db.execute("DELETE FROM habit_logs WHERE id = ?", (row["id"],))
        done = False
    else:
        db.execute(
            """INSERT INTO habit_logs (habit_id, date, done, created_at) VALUES (?, ?, 1, ?)
               ON CONFLICT(habit_id, date) DO UPDATE SET done = 1""",
            (habit_id, date_str, datetime.now().isoformat()),
        )
        done = True
    db.commit()

    done_dates = habit_done_dates(habit_id)
    streak, best = streak_metrics(
        done_dates, habit["days"], bool(habit["imperfection"]), date.today()
    )
    new_badges = award_badges(session["user_id"], habit_id, best) if done else []
    dispatch_webhook(current_user(), {
        "event": "habit.checked" if done else "habit.unchecked",
        "habit_id": habit_id,
        "title": habit["title"],
        "date": date_str,
        "done": done,
        "streak": streak,
        "best": best,
    })
    return jsonify({
        "done": done,
        "streak": streak,
        "best": best,
        "total": len(done_dates),
        "imperfection": bool(habit["imperfection"]),
        "quote": random_quote() if done else None,
        "new_badges": new_badges,
    })


@app.route("/api/reminders")
@login_required
def api_reminders():
    user = current_user()
    today = date.today()
    habits = get_db().execute(
        "SELECT * FROM habits WHERE user_id = ? AND archived = 0",
        (session["user_id"],),
    ).fetchall()

    pending = []
    for h in habits:
        if not scheduled_on(h["days"], today):
            continue
        row = get_db().execute(
            "SELECT done FROM habit_logs WHERE habit_id = ? AND date = ?",
            (h["id"], today.isoformat()),
        ).fetchone()
        if not row or not row["done"]:
            pending.append({"id": h["id"], "title": h["title"], "icon": h["icon"],
                            "time": h["remind_time"]})

    return jsonify({
        "enabled": bool(user["notifications_enabled"]),
        "timezone": user["timezone"] if "timezone" in user.keys() else DEFAULT_TZ,
        "pending": pending,
        "date": today.isoformat(),
    })


@app.route("/stats")
@login_required
def stats():
    uid = session["user_id"]
    period = request.args.get("period", "week")
    habits = get_db().execute(
        "SELECT * FROM habits WHERE user_id = ? AND archived = 0 ORDER BY sort_order, id",
        (uid,),
    ).fetchall()

    report = period_report(habits, period, date.today())

    habit_ids = [h["id"] for h in habits]
    done_pairs = set()
    if habit_ids:
        placeholders = ",".join("?" * len(habit_ids))
        rows = get_db().execute(
            f"SELECT habit_id, date FROM habit_logs WHERE done = 1 "
            f"AND habit_id IN ({placeholders})",
            tuple(habit_ids),
        ).fetchall()
        done_pairs = {(r["habit_id"], r["date"]) for r in rows}

    return render_template(
        "stats.html",
        report=report,
        cats=category_report(habits, done_pairs, uid),
        tags_map=habit_tags_map(habit_ids),
        habit_count=len(habits),
    )


@app.route("/categories", methods=["GET", "POST"])
@login_required
def categories():
    db = get_db()
    uid = session["user_id"]
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Введите название категории", "error")
        else:
            db.execute(
                "INSERT INTO categories (user_id, name, color, icon, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, name, request.form.get("color") or "#6c5ce7",
                 request.form.get("icon") or "🏷", datetime.now().isoformat()),
            )
            db.commit()
            flash("Категория добавлена", "success")
        return redirect(url_for("categories"))

    rows = db.execute(
        "SELECT category_id, COUNT(*) AS c FROM habits "
        "WHERE user_id = ? AND archived = 0 GROUP BY category_id",
        (uid,),
    ).fetchall()
    counts = {r["category_id"]: r["c"] for r in rows}
    return render_template("categories.html", categories=user_categories(uid), counts=counts)


@app.route("/categories/<int:cat_id>/delete", methods=["POST"])
@login_required
def category_delete(cat_id):
    db = get_db()
    db.execute(
        "DELETE FROM categories WHERE id = ? AND user_id = ?",
        (cat_id, session["user_id"]),
    )
    db.commit()
    flash("Категория удалена", "success")
    return redirect(url_for("categories"))


@app.route("/export/csv")
@login_required
def export_csv():
    uid = session["user_id"]
    rows = get_db().execute(
        """SELECT h.id AS habit_id, h.title AS habit, c.name AS category,
                  l.date AS date, l.created_at AS created_at
           FROM habit_logs l
           JOIN habits h ON h.id = l.habit_id
           LEFT JOIN categories c ON c.id = h.category_id
           WHERE h.user_id = ? AND l.done = 1
           ORDER BY l.date, h.title""",
        (uid,),
    ).fetchall()
    tags_map = habit_tags_map([r["habit_id"] for r in rows])

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["Привычка", "Категория", "Теги", "Дата", "Отметка создана"])
    for r in rows:
        writer.writerow([
            r["habit"], r["category"] or "",
            ", ".join(tags_map.get(r["habit_id"], [])),
            r["date"], r["created_at"],
        ])

    data = "\ufeff" + buf.getvalue()
    filename = f"habitflow_{date.today().isoformat()}.csv"
    return Response(
        data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/achievements")
@login_required
def achievements():
    uid = session["user_id"]
    db = get_db()
    habits = db.execute(
        "SELECT * FROM habits WHERE user_id = ? AND archived = 0 ORDER BY sort_order, id",
        (uid,),
    ).fetchall()

    rows = db.execute(
        """SELECT ba.code, ba.earned_at, h.title AS habit, h.icon AS habit_icon,
                  h.color AS habit_color
           FROM badge_awards ba JOIN habits h ON h.id = ba.habit_id
           WHERE ba.user_id = ? ORDER BY ba.earned_at DESC""",
        (uid,),
    ).fetchall()
    earned = [dict(BADGE_MAP[r["code"]],
                   habit=r["habit"], habit_icon=r["habit_icon"],
                   habit_color=r["habit_color"], earned_at=r["earned_at"])
              for r in rows if r["code"] in BADGE_MAP]

    progress = []
    for h in habits:
        st = habit_stats(h)
        nxt = next((b for b in BADGES if st["best"] < b[1]), None)
        progress.append({
            "habit": h, "streak": st["streak"], "best": st["best"],
            "next": nxt, "imperfection": st["imperfection"],
        })

    return render_template(
        "achievements.html",
        badges=BADGES,
        earned=earned,
        progress=progress,
        total_earned=len(earned),
    )


@app.route("/widget")
@login_required
def widget():
    uid = session["user_id"]
    today = date.today()
    habits = get_db().execute(
        "SELECT * FROM habits WHERE user_id = ? AND archived = 0 ORDER BY sort_order, id",
        (uid,),
    ).fetchall()
    items = []
    for h in habits:
        if not scheduled_on(h["days"], today):
            continue
        items.append({"habit": h, "stats": habit_stats(h)})
    done_count = sum(1 for i in items if i["stats"]["done_today"])
    progress = round(done_count / len(items) * 100) if items else 0
    return render_template(
        "widget.html", items=items, done_count=done_count,
        progress=progress, total=len(items),
    )


@app.route("/api/habits/<int:habit_id>/share")
@login_required
def api_share(habit_id):
    habit = get_db().execute(
        "SELECT * FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, session["user_id"]),
    ).fetchone()
    if not habit:
        return jsonify({"error": "not found"}), 404
    st = habit_stats(habit)
    text = (
        f"{habit['icon']} Я держу серию {st['streak']} дн. по привычке "
        f"«{habit['title']}» в HabitFlow! Рекорд: {st['best']} дн. 🔥"
    )
    return jsonify({"text": text, "streak": st["streak"], "best": st["best"]})


# --------------------------------------------------------------------------
# AI-коуч
# --------------------------------------------------------------------------

@app.route("/coach")
@login_required
def coach():
    data = coach_insights(session["user_id"])
    return render_template("coach.html", coach=data)


# --------------------------------------------------------------------------
# Интеграции: API-ключ, webhook, iCalendar, экспорт/импорт JSON
# --------------------------------------------------------------------------

@app.route("/integrations", methods=["GET", "POST"])
@login_required
def integrations():
    db = get_db()
    uid = session["user_id"]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "regenerate":
            regenerate_api_key(uid)
            flash("API-ключ обновлён", "success")
        elif action == "telegram_save":
            chat_id = (request.form.get("telegram_chat_id") or "").strip()
            db.execute("UPDATE users SET telegram_chat_id = ?, telegram_last_sent = NULL WHERE id = ?",
                       (chat_id or None, uid))
            db.commit()
            if chat_id:
                flash("Telegram-напоминания подключены", "success")
            else:
                flash("Telegram-напоминания отключены", "success")
        elif action == "telegram_clear":
            db.execute("UPDATE users SET telegram_chat_id = NULL, telegram_last_sent = NULL WHERE id = ?", (uid,))
            db.commit()
            flash("Telegram-напоминания отключены", "success")
        elif action == "telegram_test":
            user = current_user()
            if not telegram_configured():
                flash("На сервере не задан TELEGRAM_BOT_TOKEN", "error")
            elif not user["telegram_chat_id"]:
                flash("Сначала подключите Telegram-напоминания", "error")
            elif tg_send_message(user["telegram_chat_id"],
                                 "✅ Тестовое сообщение HabitFlow. Напоминания работают!"):
                flash("Тестовое сообщение отправлено в Telegram", "success")
            else:
                flash("Не удалось отправить. Проверьте chat ID и токен бота", "error")
        else:
            webhook = (request.form.get("webhook_url") or "").strip()
            db.execute("UPDATE users SET webhook_url = ? WHERE id = ?", (webhook or None, uid))
            db.commit()
            flash("Настройки интеграций сохранены", "success")
        return redirect(url_for("integrations"))

    key = ensure_api_key(uid)
    user = current_user()
    base = request.url_root.rstrip("/")
    return render_template(
        "integrations.html",
        api_key=key,
        webhook_url=user["webhook_url"] if user["webhook_url"] else "",
        base=base,
        telegram_chat_id=user["telegram_chat_id"] or "",
        telegram_ready=telegram_configured(),
    )


def _ics_escape(text):
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


ICS_BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


@app.route("/calendar.ics")
def calendar_ics():
    key = request.args.get("key")
    user = user_by_api_key(key) if key else current_user()
    if not user:
        return jsonify({"error": "unauthorized", "hint": "Добавьте ?key=ВАШ_API_КЛЮЧ"}), 401

    habits = get_db().execute(
        "SELECT * FROM habits WHERE user_id = ? AND archived = 0 ORDER BY sort_order, id",
        (user["id"],),
    ).fetchall()
    today = date.today()
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//HabitFlow//Habit Tracker//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:HabitFlow",
    ]
    for h in habits:
        days = sorted(parse_days(h["days"]))
        if len(days) == 7:
            rrule = "RRULE:FREQ=DAILY"
        else:
            byday = ",".join(ICS_BYDAY[d] for d in days)
            rrule = f"RRULE:FREQ=WEEKLY;BYDAY={byday}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:habit-{h['id']}@habitflow",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{today.strftime('%Y%m%d')}",
            rrule,
            f"SUMMARY:{_ics_escape(h['icon'] + ' ' + h['title'])}",
            f"DESCRIPTION:{_ics_escape(h['note'] or 'Привычка HabitFlow')}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    body = "\r\n".join(lines) + "\r\n"
    return Response(
        body,
        mimetype="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="habitflow.ics"'},
    )


@app.route("/export/json")
@login_required
def export_json():
    uid = session["user_id"]
    db = get_db()
    cats = db.execute("SELECT * FROM categories WHERE user_id = ?", (uid,)).fetchall()
    habits = db.execute(
        "SELECT * FROM habits WHERE user_id = ? ORDER BY sort_order, id", (uid,)
    ).fetchall()
    cat_name = {c["id"]: c["name"] for c in cats}
    tags_map = habit_tags_map([h["id"] for h in habits])
    index = {h["id"]: i for i, h in enumerate(habits)}

    logs = []
    if habits:
        placeholders = ",".join("?" * len(habits))
        rows = db.execute(
            f"SELECT habit_id, date, done, created_at FROM habit_logs "
            f"WHERE habit_id IN ({placeholders})", tuple(h["id"] for h in habits),
        ).fetchall()
        logs = [{"habit_i": index[r["habit_id"]], "date": r["date"],
                 "done": r["done"], "created_at": r["created_at"]} for r in rows]

    badges = db.execute(
        "SELECT habit_id, code, earned_at FROM badge_awards WHERE user_id = ?", (uid,)
    ).fetchall()
    badges = [{"habit_i": index[b["habit_id"]], "code": b["code"], "earned_at": b["earned_at"]}
              for b in badges if b["habit_id"] in index]

    payload = {
        "app": "HabitFlow",
        "version": 1,
        "exported_at": datetime.now().isoformat(),
        "categories": [{"name": c["name"], "color": c["color"], "icon": c["icon"]} for c in cats],
        "habits": [{
            "i": index[h["id"]], "title": h["title"], "note": h["note"],
            "icon": h["icon"], "color": h["color"], "days": h["days"],
            "imperfection": h["imperfection"],
            "category": cat_name.get(h["category_id"]),
            "tags": tags_map.get(h["id"], []),
            "created_at": h["created_at"], "updated_at": h["updated_at"],
        } for h in habits],
        "logs": logs,
        "badges": badges,
    }
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="habitflow_{date.today().isoformat()}.json"'},
    )


@app.route("/import/json", methods=["POST"])
@login_required
def import_json():
    uid = session["user_id"]
    raw = None
    file = request.files.get("file")
    if file:
        raw = file.read().decode("utf-8")
    elif request.form.get("payload"):
        raw = request.form.get("payload")
    if not raw:
        flash("Выберите JSON-файл для импорта", "error")
        return redirect(url_for("integrations"))
    try:
        data = json.loads(raw)
    except ValueError:
        flash("Некорректный JSON", "error")
        return redirect(url_for("integrations"))

    db = get_db()
    now = datetime.now().isoformat()
    # Полная замена данных пользователя.
    db.execute("DELETE FROM categories WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM habits WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM tags WHERE user_id = ?", (uid,))

    cat_ids = {}
    for c in data.get("categories", []):
        cur = db.execute(
            "INSERT INTO categories (user_id, name, color, icon, created_at) VALUES (?, ?, ?, ?, ?)",
            (uid, c.get("name", "Категория"), c.get("color", "#6c5ce7"), c.get("icon", "🏷"), now),
        )
        cat_ids[c.get("name")] = cur.lastrowid

    habit_ids = {}
    for h in data.get("habits", []):
        cur = db.execute(
            """INSERT INTO habits (user_id, title, note, icon, color, days, category_id,
                                    imperfection, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, h.get("title", "Привычка"), h.get("note", ""), h.get("icon", "✅"),
             h.get("color", "#6c5ce7"), h.get("days", "0,1,2,3,4,5,6"),
             cat_ids.get(h.get("category")), 1 if h.get("imperfection") else 0,
             h.get("created_at", now), h.get("updated_at", now)),
        )
        hid = cur.lastrowid
        habit_ids[h.get("i")] = hid
        for tag in h.get("tags", []):
            row = db.execute("SELECT id FROM tags WHERE user_id = ? AND name = ?", (uid, tag)).fetchone()
            tid = row["id"] if row else db.execute(
                "INSERT INTO tags (user_id, name) VALUES (?, ?)", (uid, tag)
            ).lastrowid
            db.execute("INSERT OR IGNORE INTO habit_tags (habit_id, tag_id) VALUES (?, ?)", (hid, tid))

    for lg in data.get("logs", []):
        hid = habit_ids.get(lg.get("habit_i"))
        if hid:
            db.execute(
                "INSERT OR IGNORE INTO habit_logs (habit_id, date, done, created_at) VALUES (?, ?, ?, ?)",
                (hid, lg.get("date"), lg.get("done", 1), lg.get("created_at", now)),
            )

    for b in data.get("badges", []):
        hid = habit_ids.get(b.get("habit_i"))
        if hid:
            db.execute(
                "INSERT OR IGNORE INTO badge_awards (user_id, habit_id, code, earned_at) VALUES (?, ?, ?, ?)",
                (uid, hid, b.get("code"), b.get("earned_at", now)),
            )

    db.commit()
    flash(f"Импортировано привычек: {len(data.get('habits', []))}", "success")
    return redirect(url_for("integrations"))


# --------------------------------------------------------------------------
# REST API v1 (авторизация по X-API-Key)
# --------------------------------------------------------------------------

def _habit_dict(habit):
    st = habit_stats(habit)
    return {
        "id": habit["id"],
        "title": habit["title"],
        "note": habit["note"],
        "icon": habit["icon"],
        "color": habit["color"],
        "days": sorted(parse_days(habit["days"])),
        "remind_time": habit["remind_time"],
        "remind_before": habit["remind_before"],
        "start_date": habit["start_date"],
        "category_id": habit["category_id"],
        "imperfection": bool(habit["imperfection"]),
        "tags": habit_tags_map([habit["id"]]).get(habit["id"], []),
        "streak": st["streak"],
        "best_streak": st["best"],
        "total_checkins": st["total"],
        "done_today": st["done_today"],
    }


def _api_habits(user_id):
    return get_db().execute(
        "SELECT * FROM habits WHERE user_id = ? AND archived = 0 ORDER BY sort_order, id",
        (user_id,),
    ).fetchall()


@app.route("/api/v1/me")
@api_auth
def api_v1_me():
    u = g.api_user
    return jsonify({
        "id": u["id"], "email": u["email"],
        "reminder_time": u["reminder_time"],
        "timezone": u["timezone"] if "timezone" in u.keys() else DEFAULT_TZ,
        "notifications_enabled": bool(u["notifications_enabled"]),
        "webhook_configured": bool(u["webhook_url"]),
        "telegram_linked": bool(u["telegram_chat_id"]),
    })


@app.route("/api/v1/me", methods=["PATCH", "PUT"])
@api_auth
def api_v1_me_update():
    """Обновление настроек: часовой пояс, уведомления."""
    body = request.get_json(silent=True) or {}
    fields, values = [], []
    if "timezone" in body:
        tz_name = (body.get("timezone") or DEFAULT_TZ).strip()
        if ZoneInfo is not None:
            try:
                ZoneInfo(tz_name)
            except Exception:
                return jsonify({"error": "bad timezone"}), 400
        fields.append("timezone = ?")
        values.append(tz_name)
    if "notifications_enabled" in body:
        fields.append("notifications_enabled = ?")
        values.append(1 if body["notifications_enabled"] else 0)
    if not fields:
        return jsonify({"error": "nothing to update"}), 400
    values.append(g.api_user["id"])
    db = get_db()
    db.execute("UPDATE users SET " + ", ".join(fields) + " WHERE id = ?", values)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/v1/habits")
@api_auth
def api_v1_habits():
    habits = _api_habits(g.api_user["id"])
    return jsonify({"habits": [_habit_dict(h) for h in habits], "count": len(habits)})


@app.route("/api/v1/habits", methods=["POST"])
@api_auth
def api_v1_habit_create():
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    days = body.get("days")
    if isinstance(days, list):
        days_str = ",".join(str(int(d)) for d in days) or "0,1,2,3,4,5,6"
    else:
        days_str = days or "0,1,2,3,4,5,6"
    now = datetime.now().isoformat()
    remind_before = body.get("remind_before", 10)
    try:
        remind_before = max(0, min(1440, int(remind_before)))
    except (TypeError, ValueError):
        remind_before = 10
    db = get_db()
    cur = db.execute(
        """INSERT INTO habits (user_id, title, note, icon, color, days, category_id,
                                imperfection, remind_time, remind_before, start_date,
                                created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (g.api_user["id"], title, body.get("note", ""), body.get("icon", "✅"),
         body.get("color", "#6c5ce7"), days_str, body.get("category_id"),
         1 if body.get("imperfection") else 0,
         _clean_time(body.get("remind_time")), remind_before,
         _clean_date(body.get("start_date")), now, now),
    )
    hid = cur.lastrowid
    tags = body.get("tags") or []
    if tags:
        save_habit_tags(hid, ", ".join(tags) if isinstance(tags, list) else tags, g.api_user["id"])
    db.commit()
    habit = db.execute("SELECT * FROM habits WHERE id = ?", (hid,)).fetchone()
    return jsonify(_habit_dict(habit)), 201


@app.route("/api/v1/habits/<int:habit_id>")
@api_auth
def api_v1_habit_get(habit_id):
    habit = get_db().execute(
        "SELECT * FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, g.api_user["id"]),
    ).fetchone()
    if not habit:
        return jsonify({"error": "not found"}), 404
    return jsonify(_habit_dict(habit))


@app.route("/api/v1/habits/<int:habit_id>", methods=["PATCH", "PUT"])
@api_auth
def api_v1_habit_update(habit_id):
    db = get_db()
    habit = db.execute(
        "SELECT * FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, g.api_user["id"]),
    ).fetchone()
    if not habit:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    fields, values = [], []
    for key in ("title", "note", "icon", "color", "category_id"):
        if key in body:
            fields.append(f"{key} = ?")
            values.append(body[key])
    if "days" in body:
        days = body["days"]
        fields.append("days = ?")
        values.append(",".join(str(int(d)) for d in days) if isinstance(days, list) else days)
    if "imperfection" in body:
        fields.append("imperfection = ?")
        values.append(1 if body["imperfection"] else 0)
    if "remind_time" in body:
        fields.append("remind_time = ?")
        values.append(_clean_time(body["remind_time"]))
    if "remind_before" in body:
        try:
            rb = max(0, min(1440, int(body["remind_before"])))
        except (TypeError, ValueError):
            rb = 10
        fields.append("remind_before = ?")
        values.append(rb)
    if "start_date" in body:
        fields.append("start_date = ?")
        values.append(_clean_date(body["start_date"]))
    fields.append("updated_at = ?")
    values.append(datetime.now().isoformat())
    values.extend([habit_id, g.api_user["id"]])
    db.execute(f"UPDATE habits SET {', '.join(fields)} WHERE id = ? AND user_id = ?", values)
    if "tags" in body:
        tags = body["tags"]
        save_habit_tags(habit_id, ", ".join(tags) if isinstance(tags, list) else tags, g.api_user["id"])
    db.commit()
    habit = db.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
    return jsonify(_habit_dict(habit))


@app.route("/api/v1/habits/<int:habit_id>", methods=["DELETE"])
@api_auth
def api_v1_habit_delete(habit_id):
    db = get_db()
    cur = db.execute(
        "DELETE FROM habits WHERE id = ? AND user_id = ?", (habit_id, g.api_user["id"])
    )
    db.commit()
    if not cur.rowcount:
        return jsonify({"error": "not found"}), 404
    return jsonify({"deleted": habit_id})


@app.route("/api/v1/habits/<int:habit_id>/check", methods=["POST"])
@api_auth
def api_v1_habit_check(habit_id):
    db = get_db()
    habit = db.execute(
        "SELECT * FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, g.api_user["id"]),
    ).fetchone()
    if not habit:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    date_str = body.get("date") or date.today().isoformat()
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "bad date"}), 400
    done = bool(body.get("done", True))
    if done:
        db.execute(
            "INSERT INTO habit_logs (habit_id, date, done, created_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(habit_id, date) DO UPDATE SET done = 1",
            (habit_id, date_str, datetime.now().isoformat()),
        )
    else:
        db.execute("DELETE FROM habit_logs WHERE habit_id = ? AND date = ?", (habit_id, date_str))
    db.commit()
    done_dates = habit_done_dates(habit_id)
    streak, best = streak_metrics(done_dates, habit["days"], bool(habit["imperfection"]), date.today())
    new_badges = award_badges(g.api_user["id"], habit_id, best) if done else []
    dispatch_webhook(g.api_user, {"event": "habit.checked" if done else "habit.unchecked",
                                  "habit_id": habit_id, "title": habit["title"],
                                  "date": date_str, "done": done, "streak": streak, "best": best})
    return jsonify({"habit_id": habit_id, "date": date_str, "done": done,
                    "streak": streak, "best": best, "new_badges": new_badges})


@app.route("/api/v1/habits/<int:habit_id>/snooze", methods=["POST"])
@api_auth
def api_v1_habit_snooze(habit_id):
    """Отложить напоминание (кнопка «⏰ +10 мин»)."""
    db = get_db()
    habit = db.execute(
        "SELECT * FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, g.api_user["id"]),
    ).fetchone()
    if not habit:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    try:
        minutes = max(1, min(1440, int(body.get("minutes", 10))))
    except (TypeError, ValueError):
        minutes = 10
    now_utc = datetime.now(timezone.utc)
    send_at = now_utc + timedelta(minutes=minutes)
    db.execute(
        "INSERT INTO reminder_snooze (habit_id, user_id, send_at, sent, created_at) "
        "VALUES (?, ?, ?, 0, ?)",
        (habit_id, g.api_user["id"], send_at.isoformat(), now_utc.isoformat()),
    )
    db.commit()
    return jsonify({"ok": True, "minutes": minutes, "send_at": send_at.isoformat()})


@app.route("/api/v1/habits/<int:habit_id>/skip", methods=["POST"])
@api_auth
def api_v1_habit_skip(habit_id):
    """Пропустить привычку на сегодня — глушим все напоминания по ней."""
    db = get_db()
    habit = db.execute(
        "SELECT * FROM habits WHERE id = ? AND user_id = ?",
        (habit_id, g.api_user["id"]),
    ).fetchone()
    if not habit:
        return jsonify({"error": "not found"}), 404
    day = local_date(g.api_user).isoformat()
    for slot in ("pre", "on", "r1", "r2", "r3"):
        _log_slot(db, habit_id, day, slot)
    db.execute("UPDATE reminder_snooze SET sent = 1 WHERE habit_id = ? AND sent = 0", (habit_id,))
    db.commit()
    return jsonify({"ok": True, "habit_id": habit_id, "date": day})


@app.route("/api/v1/stats")
@api_auth
def api_v1_stats():
    period = request.args.get("period", "week")
    habits = _api_habits(g.api_user["id"])
    report = period_report(habits, period, date.today())
    return jsonify({
        "period": report["period"],
        "overall_percent": report["overall"],
        "done": report["total_done"],
        "missed": report["miss"],
        "buckets": [{"label": b["label"], "done": b["done"],
                     "expected": b["expected"], "percent": b["pct"]} for b in report["buckets"]],
        "habits": [{"id": r["habit"]["id"], "title": r["habit"]["title"],
                    "percent": r["pct"], "done": r["done"], "expected": r["expected"]}
                   for r in report["per_habit"]],
    })


@app.route("/api/v1/coach")
@api_auth
def api_v1_coach():
    return jsonify(coach_insights(g.api_user["id"]))


@app.route("/api/v1/telegram/link", methods=["POST"])
@api_auth
def api_v1_telegram_link():
    """Привязка Telegram-чата к аккаунту для push-напоминаний."""
    body = request.get_json(silent=True) or {}
    chat_id = body.get("chat_id")
    if chat_id is None or str(chat_id).strip() == "":
        return jsonify({"error": "chat_id required"}), 400
    db = get_db()
    db.execute("UPDATE users SET telegram_chat_id = ?, telegram_last_sent = NULL WHERE id = ?",
               (str(chat_id).strip(), g.api_user["id"]))
    db.commit()
    return jsonify({"ok": True, "chat_id": str(chat_id).strip(),
                    "reminder_time": g.api_user["reminder_time"],
                    "notifications_enabled": bool(g.api_user["notifications_enabled"]),
                    "bot_configured": telegram_configured()})


@app.route("/api/v1/telegram/link", methods=["DELETE"])
@api_auth
def api_v1_telegram_unlink():
    db = get_db()
    db.execute("UPDATE users SET telegram_chat_id = NULL, telegram_last_sent = NULL WHERE id = ?",
               (g.api_user["id"],))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/v1/telegram/test", methods=["POST"])
@api_auth
def api_v1_telegram_test():
    if not telegram_configured():
        return jsonify({"error": "bot token not configured on server"}), 503
    if not g.api_user["telegram_chat_id"]:
        return jsonify({"error": "not linked"}), 400
    ok = tg_send_message(g.api_user["telegram_chat_id"],
                         "✅ Тестовое сообщение HabitFlow. Напоминания работают!")
    return jsonify({"ok": ok})


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    db = get_db()
    if request.method == "POST":
        tz_name = (request.form.get("timezone") or DEFAULT_TZ).strip()
        if ZoneInfo is not None:
            try:
                ZoneInfo(tz_name)
            except Exception:
                tz_name = DEFAULT_TZ
        enabled = 1 if request.form.get("notifications_enabled") else 0
        db.execute(
            "UPDATE users SET timezone = ?, notifications_enabled = ? WHERE id = ?",
            (tz_name, enabled, session["user_id"]),
        )
        db.commit()
        flash("Настройки сохранены", "success")
        return redirect(url_for("settings"))
    return render_template("settings.html", timezones=COMMON_TIMEZONES)


@app.route("/manifest.json")
def manifest():
    return app.send_static_file("manifest.json")


@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    return app.send_static_file("apple-touch-icon.png")


@app.route("/sw.js")
def service_worker():
    response = app.send_static_file("sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    return response


init_db()

# Автозапуск рассылки напоминаний в production (например, под gunicorn на Railway).
# Включается переменной HABITFLOW_ENABLE_SCHEDULER=1 и только при наличии токена бота.
if os.environ.get("HABITFLOW_ENABLE_SCHEDULER") == "1" and telegram_configured():
    start_reminder_scheduler()


# --------------------------------------------------------------------------
# Автозапуск Telegram-бота в фоновом потоке (для хостинга Amvera и т.п.)
# --------------------------------------------------------------------------

import logging
logger = logging.getLogger(__name__)

logger.info("[HabitFlow] Модуль app.py загружается")
logger.info(f"[HabitFlow] TELEGRAM_BOT_TOKEN: {'задан' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'НЕ задан'}")
logger.info(f"[HabitFlow] WERKZEUG_RUN_MAIN: {os.environ.get('WERKZEUG_RUN_MAIN', 'не установлен')}")

_bot_thread_started = False

def start_bot_background():
    """Запускает Telegram-бота в отдельном потоке, если есть токен."""
    global _bot_thread_started
    
    if _bot_thread_started:
        return
    
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.warning("[HabitFlow] TELEGRAM_BOT_TOKEN не задан — бот не запущен")
        return
    
    def _run_bot():
        try:
            # Импортируем здесь, чтобы избежать циклических зависимостей
            from telegram_bot import main as bot_main
            logger.info("[HabitFlow] Запускаю Telegram-бота в фоновом потоке...")
            bot_main()
        except Exception as e:
            logger.error(f"[HabitFlow] Ошибка запуска бота: {e}")
            import traceback
            traceback.print_exc()
    
    _bot_thread_started = True
    bot_thread = threading.Thread(target=_run_bot, daemon=True, name="telegram-bot")
    bot_thread.start()
    logger.info("[HabitFlow] Поток Telegram-бота запущен")

# Автозапуск бота при импорте модуля (для gunicorn/production)
# Запускается только если есть токен и это не reloader parent process
def _auto_start_bot():
    """Автоматически запускает бота при старте приложения."""
    # Не запускаем в reloader parent process
    if os.environ.get("WERKZEUG_RUN_MAIN") == "false":
        return
    
    # Запускаем только если есть токен
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        start_bot_background()

# Запускаем бота при импорте (для gunicorn)
_auto_start_bot()


# Для Flask dev server
if __name__ == "__main__":
    DEBUG = os.environ.get("HABITFLOW_DEBUG", "1") == "1"
    # Планировщик напоминаний запускаем только в рабочем процессе
    # (не в reloader-родителе werkzeug), и только если есть токен бота.
    if (not DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true") and telegram_configured():
        start_reminder_scheduler()
        start_bot_background()
    # На хостинге (Railway и т.п.) слушаем 0.0.0.0:$PORT, локально — 127.0.0.1:5000.
    app.run(debug=DEBUG,
            host="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1",
            port=int(os.environ.get("PORT", "5000")))
