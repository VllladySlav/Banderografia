# -*- coding: utf-8 -*-
"""Лексикографічна база даних і службові функції для вкладки «Словник».

Файл створює окрему SQLite-базу outputs/lexicographic_dictionary.sqlite.
Лексикографічна частина БД чітко відтворює 10 таблиць зі схеми:
СЛОВО, ТЛУМАЧЕННЯ, СЛОВОФОРМА, СТІЙКА СПОЛУКА, ЦИТАТА, ДЖЕРЕЛО,
ПОКЛИКАННЯ, ТЛУМАЧЕННЯ-ЦИТАТА, СЛОВОФОРМА-ЦИТАТА,
СТІЙКА СПОЛУКА-ЦИТАТА.

Окремо створено службові таблиці AUTH_USERS і DICTIONARY_OPTIONS. Вони не є
частиною лексикографічної моделі, а потрібні для авторизації та керування
випадними списками в інтерфейсі укладача.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from pathlib import Path
from typing import Iterable

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = PROJECT_DIR / "outputs"
LEX_DB_PATH = OUTPUTS_DIR / "lexicographic_dictionary.sqlite"


# У v3 випадні списки не мають наперед заповнених значень.
# Редактор сам поступово формує довідники через пункт «Додати нове значення...».
DEFAULT_OPTIONS: dict[str, list[str]] = {}

# Значення, які були автоматично засіяні у v1/v2. Під час ініціалізації v3
# вони вилучаються зі службового довідника, щоб випадні списки стартували порожніми.
OLD_PRELOADED_OPTIONS: dict[str, list[str]] = {
    "ЧАСТИНА МОВИ": [
        "іменник", "прикметник", "дієслово", "прислівник", "займенник",
        "числівник", "прийменник", "сполучник", "частка", "вигук",
        "абревіятура", "власна назва", "інше",
    ],
    "ГРАМАТИЧНІ ОЗНАКИ": [
        "невідмінюване", "варіянтне написання", "омонім", "власна назва",
        "скорочення", "абревіятура", "неусталена форма",
    ],
    "ГРАМАТИЧНІ ОЗНАКИ::іменник": [
        "чол. рід", "жін. рід", "сер. рід", "спільний рід",
        "однина", "множина", "тільки однина", "тільки множина",
        "називний відмінок", "родовий відмінок", "давальний відмінок", "знахідний відмінок",
        "орудний відмінок", "місцевий відмінок", "кличний відмінок",
        "I відміна", "II відміна", "III відміна", "IV відміна", "невідмінюване",
    ],
    "ГРАМАТИЧНІ ОЗНАКИ::прикметник": [
        "чол. рід", "жін. рід", "сер. рід", "множина",
        "називний відмінок", "родовий відмінок", "давальний відмінок", "знахідний відмінок",
        "орудний відмінок", "місцевий відмінок", "кличний відмінок",
        "якісний", "відносний", "присвійний", "тверда група", "м’яка група",
        "вищий ступінь", "найвищий ступінь", "коротка форма",
    ],
    "ГРАМАТИЧНІ ОЗНАКИ::дієслово": [
        "доконаний вид", "недоконаний вид", "двовидове", "перехідне", "неперехідне",
        "інфінітив", "особова форма", "безособова форма", "наказовий спосіб",
        "умовний спосіб", "дійсний спосіб", "теперішній час", "минулий час", "майбутній час",
        "активний стан", "пасивний стан", "зворотне", "дієприкметник", "дієприслівник",
    ],
    "ГРАМАТИЧНІ ОЗНАКИ::займенник": [
        "особовий", "зворотний", "присвійний", "вказівний", "означальний", "питальний",
        "відносний", "заперечний", "неозначений", "чол. рід", "жін. рід", "сер. рід",
        "однина", "множина", "називний відмінок", "родовий відмінок", "давальний відмінок",
        "знахідний відмінок", "орудний відмінок", "місцевий відмінок",
    ],
    "ГРАМАТИЧНІ ОЗНАКИ::числівник": [
        "кількісний", "порядковий", "збірний", "дробовий", "неозначено-кількісний",
        "простий", "складний", "складений", "називний відмінок", "родовий відмінок",
        "давальний відмінок", "знахідний відмінок", "орудний відмінок", "місцевий відмінок",
    ],
    "ГРАМАТИЧНІ ОЗНАКИ::прислівник": [
        "обставинний", "означальний", "місця", "часу", "причини", "мети", "способу дії",
        "міри і ступеня", "вищий ступінь", "найвищий ступінь", "незмінюване",
    ],
    "СТИЛІСТИКА": [
        "нейтр.", "книжн.", "публіц.", "сусп.-політ.", "ідеол.",
        "нац.-визв.", "ритор.", "оцінн.", "іст.", "організац.",
        "термінол.", "фразеол.", "заст.", "рідковж.", "емігрант.",
    ],
    "ТИП СТІЙКОЇ СПОЛУКИ": [
        "стійке словосполучення", "фразеологізм", "політична формула",
        "ідеологічне кліше", "термінологічна сполука", "номінаційна формула",
        "риторична формула", "інше",
    ],
    "СТАТУС СТАТТІ": ["чернетка", "перевірено", "опубліковано"],
}



GRAMMAR_FALLBACK_FIELD = "ГРАМАТИЧНІ ОЗНАКИ"


def grammar_field_name(pos: str) -> str:
    pos_norm = str(pos or "").strip().lower()
    if not pos_norm:
        return GRAMMAR_FALLBACK_FIELD
    return f"ГРАМАТИЧНІ ОЗНАКИ::{pos_norm}"



def lex_connect() -> sqlite3.Connection:
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEX_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_lex_db() -> None:
    conn = lex_connect()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS "ПОКЛИКАННЯ" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "СКОРОЧЕННЯ" TEXT,
            "ПОВНА НАЗВА" TEXT
        );

        CREATE TABLE IF NOT EXISTS "СЛОВО" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "РЕЄСТРОВА ОДИНИЦЯ" TEXT NOT NULL,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "ЧАСТИНА МОВИ" TEXT,
            "ГРАМАТИЧНІ ОЗНАКИ" TEXT,
            "СТИЛІСТИКА" TEXT,
            "ПОХОДЖЕННЯ" TEXT
        );

        CREATE TABLE IF NOT EXISTS "ТЛУМАЧЕННЯ" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "ТЛУМАЧЕННЯ" TEXT,
            "ПОКЛИКАННЯ_ID" INTEGER,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "СЛОВО_ID" INTEGER NOT NULL,
            "СТИЛІСТИКА" TEXT,
            FOREIGN KEY("ПОКЛИКАННЯ_ID") REFERENCES "ПОКЛИКАННЯ"("ID") ON DELETE SET NULL,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS "СЛОВОФОРМА" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "СЛОВОФОРМА" TEXT NOT NULL,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "СЛОВО_ID" INTEGER NOT NULL,
            "ЧАСТИНА МОВИ" TEXT,
            "ГРАМАТИЧНІ ОЗНАКИ" TEXT,
            "СТИЛІСТИКА" TEXT,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS "СТІЙКА СПОЛУКА" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "СЛОВО_ID" INTEGER NOT NULL,
            "ОДИНИЦЯ" TEXT NOT NULL,
            "ТИП" TEXT,
            "ТЛУМАЧЕННЯ" TEXT,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "СТИЛІСТИКА" TEXT,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS "ДЖЕРЕЛО" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "СКОРОЧЕННЯ" TEXT,
            "ПОВНА НАЗВА" TEXT
        );

        CREATE TABLE IF NOT EXISTS "ЦИТАТА" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "PRG" INTEGER,
            "SRG" INTEGER,
            "ПЕРШОДРУК" TEXT,
            "ПЕРЕДРУК" TEXT,
            "ДЖЕРЕЛО_ID" INTEGER,
            FOREIGN KEY("ДЖЕРЕЛО_ID") REFERENCES "ДЖЕРЕЛО"("ID") ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS "ТЛУМАЧЕННЯ-ЦИТАТА" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "ТЛУМАЧЕННЯ_ID" INTEGER NOT NULL,
            "ЦИТАТА_ID" INTEGER NOT NULL,
            FOREIGN KEY("ТЛУМАЧЕННЯ_ID") REFERENCES "ТЛУМАЧЕННЯ"("ID") ON DELETE CASCADE,
            FOREIGN KEY("ЦИТАТА_ID") REFERENCES "ЦИТАТА"("ID") ON DELETE CASCADE,
            UNIQUE("ТЛУМАЧЕННЯ_ID", "ЦИТАТА_ID")
        );

        CREATE TABLE IF NOT EXISTS "СЛОВОФОРМА-ЦИТАТА" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "СЛОВОФОРМА_ID" INTEGER NOT NULL,
            "ЦИТАТА_ID" INTEGER NOT NULL,
            FOREIGN KEY("СЛОВОФОРМА_ID") REFERENCES "СЛОВОФОРМА"("ID") ON DELETE CASCADE,
            FOREIGN KEY("ЦИТАТА_ID") REFERENCES "ЦИТАТА"("ID") ON DELETE CASCADE,
            UNIQUE("СЛОВОФОРМА_ID", "ЦИТАТА_ID")
        );

        CREATE TABLE IF NOT EXISTS "СТІЙКА СПОЛУКА-ЦИТАТА" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "СТІЙКА_СПОЛУКА_ID" INTEGER NOT NULL,
            "ЦИТАТА_ID" INTEGER NOT NULL,
            FOREIGN KEY("СТІЙКА_СПОЛУКА_ID") REFERENCES "СТІЙКА СПОЛУКА"("ID") ON DELETE CASCADE,
            FOREIGN KEY("ЦИТАТА_ID") REFERENCES "ЦИТАТА"("ID") ON DELETE CASCADE,
            UNIQUE("СТІЙКА_СПОЛУКА_ID", "ЦИТАТА_ID")
        );

        CREATE TABLE IF NOT EXISTS "AUTH_USERS" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "NAME" TEXT NOT NULL,
            "EMAIL" TEXT NOT NULL UNIQUE,
            "PASSWORD_SALT" BLOB NOT NULL,
            "PASSWORD_HASH" BLOB NOT NULL,
            "ROLE" TEXT NOT NULL DEFAULT 'viewer',
            "CAN_EDIT" INTEGER NOT NULL DEFAULT 0,
            "CREATED_AT" TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS "DICTIONARY_OPTIONS" (
            "ID" INTEGER PRIMARY KEY AUTOINCREMENT,
            "FIELD_NAME" TEXT NOT NULL,
            "VALUE" TEXT NOT NULL,
            UNIQUE("FIELD_NAME", "VALUE")
        );

        CREATE TABLE IF NOT EXISTS "LEX_META" (
            "KEY" TEXT PRIMARY KEY,
            "VALUE" TEXT
        );

        CREATE INDEX IF NOT EXISTS "idx_тлумачення_слово" ON "ТЛУМАЧЕННЯ"("СЛОВО_ID");
        CREATE INDEX IF NOT EXISTS "idx_словоформа_слово" ON "СЛОВОФОРМА"("СЛОВО_ID");
        CREATE INDEX IF NOT EXISTS "idx_стійка_сполука_слово" ON "СТІЙКА СПОЛУКА"("СЛОВО_ID");
        CREATE INDEX IF NOT EXISTS "idx_цитата_джерело" ON "ЦИТАТА"("ДЖЕРЕЛО_ID");
        CREATE INDEX IF NOT EXISTS "idx_слово_реєстр" ON "СЛОВО"("РЕЄСТРОВА ОДИНИЦЯ");
        CREATE INDEX IF NOT EXISTS "idx_словоформа_форма" ON "СЛОВОФОРМА"("СЛОВОФОРМА");
        """
    )
    conn.commit()

    # Міграція v3: додаємо словоформні граматичні поля до вже створеної БД,
    # якщо вона була згенерована попередніми версіями.
    existing_cols = {row[1] for row in conn.execute('PRAGMA table_info("СЛОВОФОРМА")').fetchall()}
    for col_name in ["ЧАСТИНА МОВИ", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА"]:
        if col_name not in existing_cols:
            conn.execute(f'ALTER TABLE "СЛОВОФОРМА" ADD COLUMN "{col_name}" TEXT')

    # У v3 випадні списки мають бути порожніми, окрім тих значень, які редактор
    # додасть сам. Автоматично засіяні значення з v1/v2 вилучаємо лише один раз,
    # щоб надалі не видаляти значення, які редактор спеціально додасть вручну.
    migration_key = "v3_options_cleaned"
    migration_done = conn.execute('SELECT "VALUE" FROM "LEX_META" WHERE "KEY" = ?', (migration_key,)).fetchone()
    if migration_done is None:
        for field_name, values in OLD_PRELOADED_OPTIONS.items():
            for value in values:
                conn.execute(
                    'DELETE FROM "DICTIONARY_OPTIONS" WHERE "FIELD_NAME" = ? AND "VALUE" = ?',
                    (field_name, value),
                )
        conn.execute('INSERT OR REPLACE INTO "LEX_META"("KEY", "VALUE") VALUES (?, ?)', (migration_key, "1"))

    for field_name, values in DEFAULT_OPTIONS.items():
        for value in values:
            add_option(field_name, value, conn=conn)
    conn.commit()
    conn.close()


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    if salt is None:
        salt = os.urandom(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt, hashed


def user_count() -> int:
    conn = lex_connect()
    n = conn.execute('SELECT COUNT(*) FROM "AUTH_USERS"').fetchone()[0]
    conn.close()
    return int(n)


def create_user(name: str, email: str, password: str, role: str = "viewer", can_edit: bool = False) -> int:
    salt, hashed = hash_password(password)
    conn = lex_connect()
    cur = conn.execute(
        'INSERT INTO "AUTH_USERS"("NAME", "EMAIL", "PASSWORD_SALT", "PASSWORD_HASH", "ROLE", "CAN_EDIT") VALUES (?, ?, ?, ?, ?, ?)',
        (name.strip(), email.strip().lower(), salt, hashed, role, int(can_edit)),
    )
    conn.commit()
    user_id = int(cur.lastrowid)
    conn.close()
    return user_id


def verify_user(email: str, password: str) -> dict | None:
    conn = lex_connect()
    row = conn.execute('SELECT * FROM "AUTH_USERS" WHERE lower("EMAIL") = lower(?)', (email.strip(),)).fetchone()
    conn.close()
    if row is None:
        return None
    salt = bytes(row["PASSWORD_SALT"])
    expected = bytes(row["PASSWORD_HASH"])
    _, actual = hash_password(password, salt)
    if not hmac.compare_digest(expected, actual):
        return None
    return dict(row)


def list_users() -> list[dict]:
    conn = lex_connect()
    rows = conn.execute('SELECT "ID", "NAME", "EMAIL", "ROLE", "CAN_EDIT", "CREATED_AT" FROM "AUTH_USERS" ORDER BY "CREATED_AT" DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_user_permission(user_id: int, role: str, can_edit: bool) -> None:
    conn = lex_connect()
    conn.execute('UPDATE "AUTH_USERS" SET "ROLE" = ?, "CAN_EDIT" = ? WHERE "ID" = ?', (role, int(can_edit), int(user_id)))
    conn.commit()
    conn.close()


def get_options(field_name: str) -> list[str]:
    conn = lex_connect()
    rows = conn.execute('SELECT "VALUE" FROM "DICTIONARY_OPTIONS" WHERE "FIELD_NAME" = ? ORDER BY "VALUE"', (field_name,)).fetchall()
    conn.close()
    return [str(r["VALUE"]) for r in rows]


def get_grammar_options(pos: str) -> list[str]:
    """Повертає граматичні ознаки відповідно до вибраної частини мови.

    До частиномовного списку додаються загальні граматичні / технічні позначки,
    щоб редактор міг фіксувати варіянтність, невідмінюваність, омонімію тощо.
    """
    specific = get_options(grammar_field_name(pos))
    general = get_options(GRAMMAR_FALLBACK_FIELD)
    out: list[str] = []
    seen: set[str] = set()
    for value in specific + general:
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def add_option(field_name: str, value: str, conn: sqlite3.Connection | None = None) -> None:
    value = str(value or "").strip()
    if not value:
        return
    close = False
    if conn is None:
        conn = lex_connect()
        close = True
    conn.execute('INSERT OR IGNORE INTO "DICTIONARY_OPTIONS"("FIELD_NAME", "VALUE") VALUES (?, ?)', (field_name, value))
    if close:
        conn.commit()
        conn.close()


def normalize_join(values: Iterable[str] | str | None) -> str:
    if values is None:
        return ""
    if isinstance(values, str):
        return values.strip()
    out = []
    seen = set()
    for v in values:
        v = str(v or "").strip()
        if v and v not in seen:
            out.append(v)
            seen.add(v)
    return "; ".join(out)


def search_words(query: str = "", limit: int = 200) -> list[dict]:
    conn = lex_connect()
    if query:
        rows = conn.execute(
            'SELECT * FROM "СЛОВО" WHERE "РЕЄСТРОВА ОДИНИЦЯ" LIKE ? ORDER BY "РЕЄСТРОВА ОДИНИЦЯ" LIMIT ?',
            (f"%{query}%", int(limit)),
        ).fetchall()
    else:
        rows = conn.execute('SELECT * FROM "СЛОВО" ORDER BY "РЕЄСТРОВА ОДИНИЦЯ" LIMIT ?', (int(limit),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_word(word_id: int) -> dict | None:
    conn = lex_connect()
    row = conn.execute('SELECT * FROM "СЛОВО" WHERE "ID" = ?', (int(word_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_word_by_register(register: str) -> dict | None:
    conn = lex_connect()
    row = conn.execute('SELECT * FROM "СЛОВО" WHERE lower("РЕЄСТРОВА ОДИНИЦЯ") = lower(?) ORDER BY "ID" LIMIT 1', (str(register or "").strip(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def recompute_word_frequency(word_id: int) -> int:
    """Перераховує частоту леми як суму частот усіх словоформ, прив’язаних до неї."""
    conn = lex_connect()
    total = conn.execute('SELECT COALESCE(SUM("ЧАСТОТА"), 0) FROM "СЛОВОФОРМА" WHERE "СЛОВО_ID" = ?', (int(word_id),)).fetchone()[0]
    conn.execute('UPDATE "СЛОВО" SET "ЧАСТОТА" = ? WHERE "ID" = ?', (int(total or 0), int(word_id)))
    conn.commit()
    conn.close()
    return int(total or 0)


def save_word(word_id: int | None, register: str, frequency: int, pos: str, grammar: str, stylistics: str, origin: str) -> int:
    """Створює або оновлює лему.

    Якщо word_id не передано, але така реєстрова одиниця вже існує, функція не
    створює дубліката, а оновлює наявну статтю. Частоту леми надалі бажано
    перераховувати через recompute_word_frequency(), тобто як суму частот
    прив’язаних словоформ.
    """
    register = str(register or "").strip()
    conn = lex_connect()
    existing_id: int | None = None
    if not word_id and register:
        row = conn.execute('SELECT "ID" FROM "СЛОВО" WHERE lower("РЕЄСТРОВА ОДИНИЦЯ") = lower(?) ORDER BY "ID" LIMIT 1', (register,)).fetchone()
        if row:
            existing_id = int(row["ID"])
    target_id = int(word_id or existing_id or 0)
    if target_id:
        conn.execute(
            'UPDATE "СЛОВО" SET "РЕЄСТРОВА ОДИНИЦЯ"=?, "ЧАСТИНА МОВИ"=?, "ГРАМАТИЧНІ ОЗНАКИ"=?, "СТИЛІСТИКА"=?, "ПОХОДЖЕННЯ"=? WHERE "ID"=?',
            (register, pos, grammar, stylistics, origin, target_id),
        )
        out_id = target_id
    else:
        cur = conn.execute(
            'INSERT INTO "СЛОВО"("РЕЄСТРОВА ОДИНИЦЯ", "ЧАСТОТА", "ЧАСТИНА МОВИ", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА", "ПОХОДЖЕННЯ") VALUES (?, ?, ?, ?, ?, ?)',
            (register, int(frequency or 0), pos, grammar, stylistics, origin),
        )
        out_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return out_id


def upsert_reference(table: str, abbr: str, full_title: str) -> int:
    if table not in {"ДЖЕРЕЛО", "ПОКЛИКАННЯ"}:
        raise ValueError("table must be ДЖЕРЕЛО or ПОКЛИКАННЯ")
    abbr = str(abbr or "").strip()
    full_title = str(full_title or "").strip()
    conn = lex_connect()
    row = conn.execute(f'SELECT "ID" FROM "{table}" WHERE "СКОРОЧЕННЯ" = ? AND "ПОВНА НАЗВА" = ?', (abbr, full_title)).fetchone()
    if row:
        out_id = int(row["ID"])
    else:
        cur = conn.execute(f'INSERT INTO "{table}"("СКОРОЧЕННЯ", "ПОВНА НАЗВА") VALUES (?, ?)', (abbr, full_title))
        out_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return out_id


def upsert_wordform(
    word_id: int,
    wordform: str,
    frequency: int,
    pos: str = "",
    grammar: str = "",
    stylistics: str = "",
) -> int:
    """Створює або оновлює словоформу, зберігаючи її власні граматичні ознаки.

    Граматика словоформи не дублює автоматично граматику леми: вона призначена
    для фіксації конкретної текстової реалізації, наприклад відмінка, числа,
    роду, часу чи способу саме в обраному контекстному матеріалі.
    """
    conn = lex_connect()
    row = conn.execute(
        'SELECT "ID" FROM "СЛОВОФОРМА" WHERE "СЛОВО_ID" = ? AND "СЛОВОФОРМА" = ?',
        (int(word_id), wordform),
    ).fetchone()
    if row:
        out_id = int(row["ID"])
        conn.execute(
            'UPDATE "СЛОВОФОРМА" SET "ЧАСТОТА" = ?, "ЧАСТИНА МОВИ" = ?, "ГРАМАТИЧНІ ОЗНАКИ" = ?, "СТИЛІСТИКА" = ? WHERE "ID" = ?',
            (int(frequency or 0), pos, grammar, stylistics, out_id),
        )
    else:
        cur = conn.execute(
            'INSERT INTO "СЛОВОФОРМА"("СЛОВОФОРМА", "ЧАСТОТА", "СЛОВО_ID", "ЧАСТИНА МОВИ", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА") VALUES (?, ?, ?, ?, ?, ?)',
            (wordform, int(frequency or 0), int(word_id), pos, grammar, stylistics),
        )
        out_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    recompute_word_frequency(int(word_id))
    return out_id


def get_wordform(word_id: int, wordform: str) -> dict | None:
    conn = lex_connect()
    row = conn.execute(
        'SELECT * FROM "СЛОВОФОРМА" WHERE "СЛОВО_ID" = ? AND "СЛОВОФОРМА" = ? ORDER BY "ID" LIMIT 1',
        (int(word_id), str(wordform or "")),
    ).fetchone()
    conn.close()
    return dict(row) if row else None



def upsert_quote(prg: int | None, srg: int | None, firstprint: str, reprint: str, source_id: int | None) -> int:
    firstprint = str(firstprint or "").strip()
    reprint = str(reprint or "").strip()
    conn = lex_connect()
    row = conn.execute(
        'SELECT "ID" FROM "ЦИТАТА" WHERE COALESCE("PRG", -1)=COALESCE(?, -1) AND COALESCE("ДЖЕРЕЛО_ID", -1)=COALESCE(?, -1) AND "ПЕРШОДРУК" = ?',
        (prg, source_id, firstprint),
    ).fetchone()
    if row:
        out_id = int(row["ID"])
    else:
        cur = conn.execute(
            'INSERT INTO "ЦИТАТА"("PRG", "SRG", "ПЕРШОДРУК", "ПЕРЕДРУК", "ДЖЕРЕЛО_ID") VALUES (?, ?, ?, ?, ?)',
            (prg, srg, firstprint, reprint, source_id),
        )
        out_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return out_id


def link_wordform_quote(wordform_id: int, quote_id: int) -> None:
    conn = lex_connect()
    conn.execute('INSERT OR IGNORE INTO "СЛОВОФОРМА-ЦИТАТА"("СЛОВОФОРМА_ID", "ЦИТАТА_ID") VALUES (?, ?)', (int(wordform_id), int(quote_id)))
    conn.commit()
    conn.close()


def link_definition_quote(definition_id: int, quote_id: int) -> None:
    conn = lex_connect()
    conn.execute('INSERT OR IGNORE INTO "ТЛУМАЧЕННЯ-ЦИТАТА"("ТЛУМАЧЕННЯ_ID", "ЦИТАТА_ID") VALUES (?, ?)', (int(definition_id), int(quote_id)))
    conn.commit()
    conn.close()


def link_collocation_quote(collocation_id: int, quote_id: int) -> None:
    conn = lex_connect()
    conn.execute('INSERT OR IGNORE INTO "СТІЙКА СПОЛУКА-ЦИТАТА"("СТІЙКА_СПОЛУКА_ID", "ЦИТАТА_ID") VALUES (?, ?)', (int(collocation_id), int(quote_id)))
    conn.commit()
    conn.close()


def insert_definition(word_id: int, text: str, reference_id: int | None, frequency: int, stylistics: str) -> int:
    conn = lex_connect()
    cur = conn.execute(
        'INSERT INTO "ТЛУМАЧЕННЯ"("ТЛУМАЧЕННЯ", "ПОКЛИКАННЯ_ID", "ЧАСТОТА", "СЛОВО_ID", "СТИЛІСТИКА") VALUES (?, ?, ?, ?, ?)',
        (text.strip(), reference_id, int(frequency or 0), int(word_id), stylistics),
    )
    conn.commit()
    out_id = int(cur.lastrowid)
    conn.close()
    return out_id


def insert_collocation(word_id: int, unit: str, unit_type: str, definition: str, frequency: int, stylistics: str) -> int:
    conn = lex_connect()
    cur = conn.execute(
        'INSERT INTO "СТІЙКА СПОЛУКА"("СЛОВО_ID", "ОДИНИЦЯ", "ТИП", "ТЛУМАЧЕННЯ", "ЧАСТОТА", "СТИЛІСТИКА") VALUES (?, ?, ?, ?, ?, ?)',
        (int(word_id), unit.strip(), unit_type, definition.strip(), int(frequency or 0), stylistics),
    )
    conn.commit()
    out_id = int(cur.lastrowid)
    conn.close()
    return out_id


def get_dictionary_article(word_id: int) -> dict[str, list[dict] | dict | None]:
    conn = lex_connect()
    word = conn.execute('SELECT * FROM "СЛОВО" WHERE "ID" = ?', (int(word_id),)).fetchone()
    definitions = conn.execute(
        'SELECT t.*, p."СКОРОЧЕННЯ" AS "ПОКЛИКАННЯ_СКОРОЧЕННЯ", p."ПОВНА НАЗВА" AS "ПОКЛИКАННЯ_ПОВНА НАЗВА" '
        'FROM "ТЛУМАЧЕННЯ" t LEFT JOIN "ПОКЛИКАННЯ" p ON p."ID" = t."ПОКЛИКАННЯ_ID" '
        'WHERE t."СЛОВО_ID" = ? ORDER BY t."ID"',
        (int(word_id),),
    ).fetchall()
    wordforms = conn.execute('SELECT * FROM "СЛОВОФОРМА" WHERE "СЛОВО_ID" = ? ORDER BY "СЛОВОФОРМА"', (int(word_id),)).fetchall()
    collocations = conn.execute('SELECT * FROM "СТІЙКА СПОЛУКА" WHERE "СЛОВО_ID" = ? ORDER BY "ОДИНИЦЯ"', (int(word_id),)).fetchall()
    quotes = conn.execute(
        'SELECT DISTINCT c.*, d."СКОРОЧЕННЯ" AS "ДЖЕРЕЛО_СКОРОЧЕННЯ", d."ПОВНА НАЗВА" AS "ДЖЕРЕЛО_ПОВНА НАЗВА" '
        'FROM "ЦИТАТА" c '
        'LEFT JOIN "ДЖЕРЕЛО" d ON d."ID" = c."ДЖЕРЕЛО_ID" '
        'LEFT JOIN "СЛОВОФОРМА-ЦИТАТА" sc ON sc."ЦИТАТА_ID" = c."ID" '
        'LEFT JOIN "СЛОВОФОРМА" sf ON sf."ID" = sc."СЛОВОФОРМА_ID" '
        'WHERE sf."СЛОВО_ID" = ? ORDER BY c."ID" DESC',
        (int(word_id),),
    ).fetchall()
    conn.close()
    return {
        "word": dict(word) if word else None,
        "definitions": [dict(r) for r in definitions],
        "wordforms": [dict(r) for r in wordforms],
        "collocations": [dict(r) for r in collocations],
        "quotes": [dict(r) for r in quotes],
    }


def get_definition_quotes(definition_id: int) -> list[dict]:
    conn = lex_connect()
    rows = conn.execute(
        'SELECT c.*, d."СКОРОЧЕННЯ" AS "ДЖЕРЕЛО_СКОРОЧЕННЯ", d."ПОВНА НАЗВА" AS "ДЖЕРЕЛО_ПОВНА НАЗВА" '
        'FROM "ЦИТАТА" c '
        'LEFT JOIN "ДЖЕРЕЛО" d ON d."ID" = c."ДЖЕРЕЛО_ID" '
        'JOIN "ТЛУМАЧЕННЯ-ЦИТАТА" tc ON tc."ЦИТАТА_ID" = c."ID" '
        'WHERE tc."ТЛУМАЧЕННЯ_ID" = ? ORDER BY c."ID"',
        (int(definition_id),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_collocation_quotes(collocation_id: int) -> list[dict]:
    conn = lex_connect()
    rows = conn.execute(
        'SELECT c.*, d."СКОРОЧЕННЯ" AS "ДЖЕРЕЛО_СКОРОЧЕННЯ", d."ПОВНА НАЗВА" AS "ДЖЕРЕЛО_ПОВНА НАЗВА" '
        'FROM "ЦИТАТА" c '
        'LEFT JOIN "ДЖЕРЕЛО" d ON d."ID" = c."ДЖЕРЕЛО_ID" '
        'JOIN "СТІЙКА СПОЛУКА-ЦИТАТА" sc ON sc."ЦИТАТА_ID" = c."ID" '
        'WHERE sc."СТІЙКА_СПОЛУКА_ID" = ? ORDER BY c."ID"',
        (int(collocation_id),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
