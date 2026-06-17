# -*- coding: utf-8 -*-
"""Лексикографічна база даних і службові функції для вкладки «Словник».

Ця версія підтримує два режими роботи:
1) локальний SQLite, якщо зовнішню базу не налаштовано;
2) зовнішній PostgreSQL / Supabase / Neon, якщо задано LEX_DATABASE_URL або DATABASE_URL.

Для Streamlit Community Cloud рекомендовано зберігати рядок підключення в Secrets:

LEX_DATABASE_URL = "postgresql+psycopg2://USER:PASSWORD@HOST:PORT/DBNAME?sslmode=require"

Основна корпусна база concordance_index.sqlite залишається локальною й read-only.
У зовнішнє сховище винесено тільки лексикографічну БД вкладки «Словник».
"""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.pool import NullPool

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


def _secret_value(name: str) -> str:
    """Безпечно дістає секрет зі Streamlit, якщо застосунок запущено у Streamlit."""
    try:
        import streamlit as st  # type: ignore

        # Плоский формат у Secrets: LEX_DATABASE_URL = "..."
        if name in st.secrets:
            return str(st.secrets[name]).strip()

        # Вкладений формат у Secrets: [database] url = "..."
        if "database" in st.secrets and isinstance(st.secrets["database"], dict):
            section = st.secrets["database"]
            for key in (name, name.lower(), "url", "URL", "connection_string"):
                if key in section:
                    return str(section[key]).strip()
    except Exception:
        pass
    return ""


def get_database_url() -> str:
    """Повертає зовнішній URL БД або порожній рядок для локального SQLite."""
    return (
        os.environ.get("LEX_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
        or _secret_value("LEX_DATABASE_URL")
        or _secret_value("DATABASE_URL")
    )


def _normalize_database_url(url: str) -> str:
    url = str(url or "").strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def _make_engine() -> Engine:
    url = _normalize_database_url(get_database_url())
    if not url:
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        return create_engine(f"sqlite:///{LEX_DB_PATH}", future=True)

    if url.startswith("postgresql"):
        # NullPool добре підходить для Streamlit Cloud / Supabase pooler:
        # не тримаємо зайві довгі з'єднання між перезапусками скрипта.
        return create_engine(url, future=True, poolclass=NullPool, pool_pre_ping=True)

    return create_engine(url, future=True, pool_pre_ping=True)


ENGINE: Engine = _make_engine()
IS_POSTGRES = ENGINE.url.get_backend_name().startswith("postgresql")
IS_SQLITE = ENGINE.url.get_backend_name().startswith("sqlite")


def using_external_db() -> bool:
    return not IS_SQLITE


def database_backend_label() -> str:
    return "PostgreSQL" if IS_POSTGRES else "SQLite"


def lex_connect() -> Connection:
    conn = ENGINE.connect()
    if IS_SQLITE:
        conn.execute(text("PRAGMA foreign_keys = ON"))
    return conn


def _row_to_dict(row: Any) -> dict | None:
    if row is None:
        return None
    return dict(row._mapping)


def _fetchone(conn: Connection, sql: str, params: dict[str, Any] | None = None) -> dict | None:
    row = conn.execute(text(sql), params or {}).mappings().first()
    return dict(row) if row else None


def _fetchall(conn: Connection, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    rows = conn.execute(text(sql), params or {}).mappings().all()
    return [dict(r) for r in rows]


def _scalar(conn: Connection, sql: str, params: dict[str, Any] | None = None) -> Any:
    return conn.execute(text(sql), params or {}).scalar()


def _insert_ignore_sql(table: str, columns: list[str], conflict_columns: list[str]) -> str:
    quoted_cols = ", ".join(f'"{c}"' for c in columns)
    values = ", ".join(f":{c}" for c in columns)
    if IS_POSTGRES:
        conflict = ", ".join(f'"{c}"' for c in conflict_columns)
        return f'INSERT INTO "{table}"({quoted_cols}) VALUES ({values}) ON CONFLICT ({conflict}) DO NOTHING'
    return f'INSERT OR IGNORE INTO "{table}"({quoted_cols}) VALUES ({values})'


def _last_insert_id(conn: Connection) -> int:
    if IS_SQLITE:
        return int(_scalar(conn, "SELECT last_insert_rowid()") or 0)
    raise RuntimeError("last_insert_id() used for non-SQLite backend")


def _run_schema_statements(conn: Connection, statements: list[str]) -> None:
    for statement in statements:
        conn.execute(text(statement))


def init_lex_db() -> None:
    id_type = "SERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    blob_type = "BYTEA" if IS_POSTGRES else "BLOB"
    created_at_type = "TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP" if IS_POSTGRES else "TEXT DEFAULT CURRENT_TIMESTAMP"

    statements = [
        f'''
        CREATE TABLE IF NOT EXISTS "ПОКЛИКАННЯ" (
            "ID" {id_type},
            "СКОРОЧЕННЯ" TEXT,
            "ПОВНА НАЗВА" TEXT
        )
        ''',
        f'''
        CREATE TABLE IF NOT EXISTS "СЛОВО" (
            "ID" {id_type},
            "РЕЄСТРОВА ОДИНИЦЯ" TEXT NOT NULL,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "ЧАСТИНА МОВИ" TEXT,
            "ГРАМАТИЧНІ ОЗНАКИ" TEXT,
            "СТИЛІСТИКА" TEXT,
            "ПОХОДЖЕННЯ" TEXT
        )
        ''',
        f'''
        CREATE TABLE IF NOT EXISTS "ТЛУМАЧЕННЯ" (
            "ID" {id_type},
            "ТЛУМАЧЕННЯ" TEXT,
            "ПОКЛИКАННЯ_ID" INTEGER,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "СЛОВО_ID" INTEGER NOT NULL,
            "СТИЛІСТИКА" TEXT,
            FOREIGN KEY("ПОКЛИКАННЯ_ID") REFERENCES "ПОКЛИКАННЯ"("ID") ON DELETE SET NULL,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE
        )
        ''',
        f'''
        CREATE TABLE IF NOT EXISTS "СЛОВОФОРМА" (
            "ID" {id_type},
            "СЛОВОФОРМА" TEXT NOT NULL,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "СЛОВО_ID" INTEGER NOT NULL,
            "ЧАСТИНА МОВИ" TEXT,
            "ГРАМАТИЧНІ ОЗНАКИ" TEXT,
            "СТИЛІСТИКА" TEXT,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE
        )
        ''',
        f'''
        CREATE TABLE IF NOT EXISTS "СТІЙКА СПОЛУКА" (
            "ID" {id_type},
            "СЛОВО_ID" INTEGER NOT NULL,
            "ОДИНИЦЯ" TEXT NOT NULL,
            "ТИП" TEXT,
            "ТЛУМАЧЕННЯ" TEXT,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "СТИЛІСТИКА" TEXT,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE
        )
        ''',
        f'''
        CREATE TABLE IF NOT EXISTS "ДЖЕРЕЛО" (
            "ID" {id_type},
            "СКОРОЧЕННЯ" TEXT,
            "ПОВНА НАЗВА" TEXT
        )
        ''',
        f'''
        CREATE TABLE IF NOT EXISTS "ЦИТАТА" (
            "ID" {id_type},
            "PRG" INTEGER,
            "SRG" INTEGER,
            "ПЕРШОДРУК" TEXT,
            "ПЕРЕДРУК" TEXT,
            "ДЖЕРЕЛО_ID" INTEGER,
            FOREIGN KEY("ДЖЕРЕЛО_ID") REFERENCES "ДЖЕРЕЛО"("ID") ON DELETE SET NULL
        )
        ''',
        f'''
        CREATE TABLE IF NOT EXISTS "ТЛУМАЧЕННЯ-ЦИТАТА" (
            "ID" {id_type},
            "ТЛУМАЧЕННЯ_ID" INTEGER NOT NULL,
            "ЦИТАТА_ID" INTEGER NOT NULL,
            FOREIGN KEY("ТЛУМАЧЕННЯ_ID") REFERENCES "ТЛУМАЧЕННЯ"("ID") ON DELETE CASCADE,
            FOREIGN KEY("ЦИТАТА_ID") REFERENCES "ЦИТАТА"("ID") ON DELETE CASCADE,
            UNIQUE("ТЛУМАЧЕННЯ_ID", "ЦИТАТА_ID")
        )
        ''',
        f'''
        CREATE TABLE IF NOT EXISTS "СЛОВОФОРМА-ЦИТАТА" (
            "ID" {id_type},
            "СЛОВОФОРМА_ID" INTEGER NOT NULL,
            "ЦИТАТА_ID" INTEGER NOT NULL,
            FOREIGN KEY("СЛОВОФОРМА_ID") REFERENCES "СЛОВОФОРМА"("ID") ON DELETE CASCADE,
            FOREIGN KEY("ЦИТАТА_ID") REFERENCES "ЦИТАТА"("ID") ON DELETE CASCADE,
            UNIQUE("СЛОВОФОРМА_ID", "ЦИТАТА_ID")
        )
        ''',
        f'''
        CREATE TABLE IF NOT EXISTS "СТІЙКА СПОЛУКА-ЦИТАТА" (
            "ID" {id_type},
            "СТІЙКА_СПОЛУКА_ID" INTEGER NOT NULL,
            "ЦИТАТА_ID" INTEGER NOT NULL,
            FOREIGN KEY("СТІЙКА_СПОЛУКА_ID") REFERENCES "СТІЙКА СПОЛУКА"("ID") ON DELETE CASCADE,
            FOREIGN KEY("ЦИТАТА_ID") REFERENCES "ЦИТАТА"("ID") ON DELETE CASCADE,
            UNIQUE("СТІЙКА_СПОЛУКА_ID", "ЦИТАТА_ID")
        )
        ''',
        f'''
        CREATE TABLE IF NOT EXISTS "AUTH_USERS" (
            "ID" {id_type},
            "NAME" TEXT NOT NULL,
            "EMAIL" TEXT NOT NULL UNIQUE,
            "PASSWORD_SALT" {blob_type} NOT NULL,
            "PASSWORD_HASH" {blob_type} NOT NULL,
            "ROLE" TEXT NOT NULL DEFAULT 'viewer',
            "CAN_EDIT" INTEGER NOT NULL DEFAULT 0,
            "CREATED_AT" {created_at_type}
        )
        ''',
        f'''
        CREATE TABLE IF NOT EXISTS "DICTIONARY_OPTIONS" (
            "ID" {id_type},
            "FIELD_NAME" TEXT NOT NULL,
            "VALUE" TEXT NOT NULL,
            UNIQUE("FIELD_NAME", "VALUE")
        )
        ''',
        '''
        CREATE TABLE IF NOT EXISTS "LEX_META" (
            "KEY" TEXT PRIMARY KEY,
            "VALUE" TEXT
        )
        ''',
        'CREATE INDEX IF NOT EXISTS "idx_тлумачення_слово" ON "ТЛУМАЧЕННЯ"("СЛОВО_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_словоформа_слово" ON "СЛОВОФОРМА"("СЛОВО_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_стійка_сполука_слово" ON "СТІЙКА СПОЛУКА"("СЛОВО_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_цитата_джерело" ON "ЦИТАТА"("ДЖЕРЕЛО_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_слово_реєстр" ON "СЛОВО"("РЕЄСТРОВА ОДИНИЦЯ")',
        'CREATE INDEX IF NOT EXISTS "idx_словоформа_форма" ON "СЛОВОФОРМА"("СЛОВОФОРМА")',
    ]

    with ENGINE.begin() as conn:
        if IS_SQLITE:
            conn.execute(text("PRAGMA foreign_keys = ON"))
        _run_schema_statements(conn, statements)

        if IS_POSTGRES:
            for col_name in ["ЧАСТИНА МОВИ", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА"]:
                conn.execute(text(f'ALTER TABLE "СЛОВОФОРМА" ADD COLUMN IF NOT EXISTS "{col_name}" TEXT'))
        else:
            rows = conn.execute(text('PRAGMA table_info("СЛОВОФОРМА")')).fetchall()
            existing_cols = {row[1] for row in rows}
            for col_name in ["ЧАСТИНА МОВИ", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА"]:
                if col_name not in existing_cols:
                    conn.execute(text(f'ALTER TABLE "СЛОВОФОРМА" ADD COLUMN "{col_name}" TEXT'))

        migration_key = "v3_options_cleaned"
        migration_done = _fetchone(conn, 'SELECT "VALUE" FROM "LEX_META" WHERE "KEY" = :key', {"key": migration_key})
        if migration_done is None:
            for field_name, values in OLD_PRELOADED_OPTIONS.items():
                for value in values:
                    conn.execute(
                        text('DELETE FROM "DICTIONARY_OPTIONS" WHERE "FIELD_NAME" = :field_name AND "VALUE" = :value'),
                        {"field_name": field_name, "value": value},
                    )
            if IS_POSTGRES:
                conn.execute(
                    text('INSERT INTO "LEX_META"("KEY", "VALUE") VALUES (:key, :value) ON CONFLICT ("KEY") DO UPDATE SET "VALUE" = EXCLUDED."VALUE"'),
                    {"key": migration_key, "value": "1"},
                )
            else:
                conn.execute(
                    text('INSERT OR REPLACE INTO "LEX_META"("KEY", "VALUE") VALUES (:key, :value)'),
                    {"key": migration_key, "value": "1"},
                )

        for field_name, values in DEFAULT_OPTIONS.items():
            for value in values:
                add_option(field_name, value, conn=conn)


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    if salt is None:
        salt = os.urandom(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt, hashed


def user_count() -> int:
    with ENGINE.begin() as conn:
        return int(_scalar(conn, 'SELECT COUNT(*) FROM "AUTH_USERS"') or 0)


def create_user(name: str, email: str, password: str, role: str = "viewer", can_edit: bool = False) -> int:
    salt, hashed = hash_password(password)
    params = {
        "name": name.strip(),
        "email": email.strip().lower(),
        "salt": salt,
        "hashed": hashed,
        "role": role,
        "can_edit": int(can_edit),
    }
    with ENGINE.begin() as conn:
        if IS_POSTGRES:
            user_id = _scalar(
                conn,
                'INSERT INTO "AUTH_USERS"("NAME", "EMAIL", "PASSWORD_SALT", "PASSWORD_HASH", "ROLE", "CAN_EDIT") '
                'VALUES (:name, :email, :salt, :hashed, :role, :can_edit) RETURNING "ID"',
                params,
            )
            return int(user_id)
        conn.execute(
            text('INSERT INTO "AUTH_USERS"("NAME", "EMAIL", "PASSWORD_SALT", "PASSWORD_HASH", "ROLE", "CAN_EDIT") '
                 'VALUES (:name, :email, :salt, :hashed, :role, :can_edit)'),
            params,
        )
        return _last_insert_id(conn)


def verify_user(email: str, password: str) -> dict | None:
    with ENGINE.begin() as conn:
        row = _fetchone(conn, 'SELECT * FROM "AUTH_USERS" WHERE lower("EMAIL") = lower(:email)', {"email": email.strip()})
    if row is None:
        return None
    salt = bytes(row["PASSWORD_SALT"])
    expected = bytes(row["PASSWORD_HASH"])
    _, actual = hash_password(password, salt)
    if not hmac.compare_digest(expected, actual):
        return None
    return row


def list_users() -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(conn, 'SELECT "ID", "NAME", "EMAIL", "ROLE", "CAN_EDIT", "CREATED_AT" FROM "AUTH_USERS" ORDER BY "CREATED_AT" DESC')


def set_user_permission(user_id: int, role: str, can_edit: bool) -> None:
    with ENGINE.begin() as conn:
        conn.execute(
            text('UPDATE "AUTH_USERS" SET "ROLE" = :role, "CAN_EDIT" = :can_edit WHERE "ID" = :user_id'),
            {"role": role, "can_edit": int(can_edit), "user_id": int(user_id)},
        )


def get_options(field_name: str) -> list[str]:
    with ENGINE.begin() as conn:
        rows = _fetchall(conn, 'SELECT "VALUE" FROM "DICTIONARY_OPTIONS" WHERE "FIELD_NAME" = :field_name ORDER BY "VALUE"', {"field_name": field_name})
    return [str(r["VALUE"]) for r in rows]


def get_grammar_options(pos: str) -> list[str]:
    """Повертає граматичні ознаки відповідно до вибраної частини мови."""
    specific = get_options(grammar_field_name(pos))
    general = get_options(GRAMMAR_FALLBACK_FIELD)
    out: list[str] = []
    seen: set[str] = set()
    for value in specific + general:
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


def add_option(field_name: str, value: str, conn: Connection | None = None) -> None:
    value = str(value or "").strip()
    if not value:
        return
    sql = _insert_ignore_sql("DICTIONARY_OPTIONS", ["FIELD_NAME", "VALUE"], ["FIELD_NAME", "VALUE"])
    params = {"FIELD_NAME": field_name, "VALUE": value}
    if conn is not None:
        conn.execute(text(sql), params)
        return
    with ENGINE.begin() as own_conn:
        own_conn.execute(text(sql), params)


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
    with ENGINE.begin() as conn:
        if query:
            if IS_POSTGRES:
                return _fetchall(
                    conn,
                    'SELECT * FROM "СЛОВО" WHERE "РЕЄСТРОВА ОДИНИЦЯ" ILIKE :query ORDER BY "РЕЄСТРОВА ОДИНИЦЯ" LIMIT :limit',
                    {"query": f"%{query}%", "limit": int(limit)},
                )
            return _fetchall(
                conn,
                'SELECT * FROM "СЛОВО" WHERE "РЕЄСТРОВА ОДИНИЦЯ" LIKE :query ORDER BY "РЕЄСТРОВА ОДИНИЦЯ" LIMIT :limit',
                {"query": f"%{query}%", "limit": int(limit)},
            )
        return _fetchall(conn, 'SELECT * FROM "СЛОВО" ORDER BY "РЕЄСТРОВА ОДИНИЦЯ" LIMIT :limit', {"limit": int(limit)})


def get_word(word_id: int) -> dict | None:
    with ENGINE.begin() as conn:
        return _fetchone(conn, 'SELECT * FROM "СЛОВО" WHERE "ID" = :word_id', {"word_id": int(word_id)})


def get_word_by_register(register: str) -> dict | None:
    with ENGINE.begin() as conn:
        return _fetchone(
            conn,
            'SELECT * FROM "СЛОВО" WHERE lower("РЕЄСТРОВА ОДИНИЦЯ") = lower(:register) ORDER BY "ID" LIMIT 1',
            {"register": str(register or "").strip()},
        )


def recompute_word_frequency(word_id: int) -> int:
    """Перераховує частоту леми як суму частот усіх словоформ, прив’язаних до неї."""
    with ENGINE.begin() as conn:
        total = int(_scalar(conn, 'SELECT COALESCE(SUM("ЧАСТОТА"), 0) FROM "СЛОВОФОРМА" WHERE "СЛОВО_ID" = :word_id', {"word_id": int(word_id)}) or 0)
        conn.execute(
            text('UPDATE "СЛОВО" SET "ЧАСТОТА" = :total WHERE "ID" = :word_id'),
            {"total": total, "word_id": int(word_id)},
        )
    return total


def save_word(word_id: int | None, register: str, frequency: int, pos: str, grammar: str, stylistics: str, origin: str) -> int:
    """Створює або оновлює лему."""
    register = str(register or "").strip()
    with ENGINE.begin() as conn:
        existing_id: int | None = None
        if not word_id and register:
            row = _fetchone(
                conn,
                'SELECT "ID" FROM "СЛОВО" WHERE lower("РЕЄСТРОВА ОДИНИЦЯ") = lower(:register) ORDER BY "ID" LIMIT 1',
                {"register": register},
            )
            if row:
                existing_id = int(row["ID"])

        target_id = int(word_id or existing_id or 0)
        params = {
            "register": register,
            "frequency": int(frequency or 0),
            "pos": pos,
            "grammar": grammar,
            "stylistics": stylistics,
            "origin": origin,
            "word_id": target_id,
        }
        if target_id:
            conn.execute(
                text('UPDATE "СЛОВО" SET "РЕЄСТРОВА ОДИНИЦЯ"=:register, "ЧАСТИНА МОВИ"=:pos, "ГРАМАТИЧНІ ОЗНАКИ"=:grammar, "СТИЛІСТИКА"=:stylistics, "ПОХОДЖЕННЯ"=:origin WHERE "ID"=:word_id'),
                params,
            )
            return target_id

        if IS_POSTGRES:
            out_id = _scalar(
                conn,
                'INSERT INTO "СЛОВО"("РЕЄСТРОВА ОДИНИЦЯ", "ЧАСТОТА", "ЧАСТИНА МОВИ", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА", "ПОХОДЖЕННЯ") '
                'VALUES (:register, :frequency, :pos, :grammar, :stylistics, :origin) RETURNING "ID"',
                params,
            )
            return int(out_id)

        conn.execute(
            text('INSERT INTO "СЛОВО"("РЕЄСТРОВА ОДИНИЦЯ", "ЧАСТОТА", "ЧАСТИНА МОВИ", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА", "ПОХОДЖЕННЯ") '
                 'VALUES (:register, :frequency, :pos, :grammar, :stylistics, :origin)'),
            params,
        )
        return _last_insert_id(conn)


def upsert_reference(table: str, abbr: str, full_title: str) -> int:
    if table not in {"ДЖЕРЕЛО", "ПОКЛИКАННЯ"}:
        raise ValueError("table must be ДЖЕРЕЛО or ПОКЛИКАННЯ")
    abbr = str(abbr or "").strip()
    full_title = str(full_title or "").strip()
    with ENGINE.begin() as conn:
        row = _fetchone(
            conn,
            f'SELECT "ID" FROM "{table}" WHERE "СКОРОЧЕННЯ" = :abbr AND "ПОВНА НАЗВА" = :full_title',
            {"abbr": abbr, "full_title": full_title},
        )
        if row:
            return int(row["ID"])
        if IS_POSTGRES:
            out_id = _scalar(
                conn,
                f'INSERT INTO "{table}"("СКОРОЧЕННЯ", "ПОВНА НАЗВА") VALUES (:abbr, :full_title) RETURNING "ID"',
                {"abbr": abbr, "full_title": full_title},
            )
            return int(out_id)
        conn.execute(
            text(f'INSERT INTO "{table}"("СКОРОЧЕННЯ", "ПОВНА НАЗВА") VALUES (:abbr, :full_title)'),
            {"abbr": abbr, "full_title": full_title},
        )
        return _last_insert_id(conn)


def upsert_wordform(
    word_id: int,
    wordform: str,
    frequency: int,
    pos: str = "",
    grammar: str = "",
    stylistics: str = "",
) -> int:
    """Створює або оновлює словоформу, зберігаючи її власні граматичні ознаки."""
    with ENGINE.begin() as conn:
        row = _fetchone(
            conn,
            'SELECT "ID" FROM "СЛОВОФОРМА" WHERE "СЛОВО_ID" = :word_id AND "СЛОВОФОРМА" = :wordform',
            {"word_id": int(word_id), "wordform": wordform},
        )
        params = {
            "word_id": int(word_id),
            "wordform": wordform,
            "frequency": int(frequency or 0),
            "pos": pos,
            "grammar": grammar,
            "stylistics": stylistics,
        }
        if row:
            out_id = int(row["ID"])
            params["id"] = out_id
            conn.execute(
                text('UPDATE "СЛОВОФОРМА" SET "ЧАСТОТА" = :frequency, "ЧАСТИНА МОВИ" = :pos, "ГРАМАТИЧНІ ОЗНАКИ" = :grammar, "СТИЛІСТИКА" = :stylistics WHERE "ID" = :id'),
                params,
            )
        else:
            if IS_POSTGRES:
                out_id = int(_scalar(
                    conn,
                    'INSERT INTO "СЛОВОФОРМА"("СЛОВОФОРМА", "ЧАСТОТА", "СЛОВО_ID", "ЧАСТИНА МОВИ", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА") '
                    'VALUES (:wordform, :frequency, :word_id, :pos, :grammar, :stylistics) RETURNING "ID"',
                    params,
                ))
            else:
                conn.execute(
                    text('INSERT INTO "СЛОВОФОРМА"("СЛОВОФОРМА", "ЧАСТОТА", "СЛОВО_ID", "ЧАСТИНА МОВИ", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА") '
                         'VALUES (:wordform, :frequency, :word_id, :pos, :grammar, :stylistics)'),
                    params,
                )
                out_id = _last_insert_id(conn)
    recompute_word_frequency(int(word_id))
    return out_id


def get_wordform(word_id: int, wordform: str) -> dict | None:
    with ENGINE.begin() as conn:
        return _fetchone(
            conn,
            'SELECT * FROM "СЛОВОФОРМА" WHERE "СЛОВО_ID" = :word_id AND "СЛОВОФОРМА" = :wordform ORDER BY "ID" LIMIT 1',
            {"word_id": int(word_id), "wordform": str(wordform or "")},
        )


def upsert_quote(prg: int | None, srg: int | None, firstprint: str, reprint: str, source_id: int | None) -> int:
    firstprint = str(firstprint or "").strip()
    reprint = str(reprint or "").strip()
    params = {"prg": prg, "srg": srg, "firstprint": firstprint, "reprint": reprint, "source_id": source_id}
    with ENGINE.begin() as conn:
        row = _fetchone(
            conn,
            'SELECT "ID" FROM "ЦИТАТА" WHERE COALESCE("PRG", -1)=COALESCE(:prg, -1) AND COALESCE("ДЖЕРЕЛО_ID", -1)=COALESCE(:source_id, -1) AND "ПЕРШОДРУК" = :firstprint',
            params,
        )
        if row:
            return int(row["ID"])
        if IS_POSTGRES:
            out_id = _scalar(
                conn,
                'INSERT INTO "ЦИТАТА"("PRG", "SRG", "ПЕРШОДРУК", "ПЕРЕДРУК", "ДЖЕРЕЛО_ID") VALUES (:prg, :srg, :firstprint, :reprint, :source_id) RETURNING "ID"',
                params,
            )
            return int(out_id)
        conn.execute(
            text('INSERT INTO "ЦИТАТА"("PRG", "SRG", "ПЕРШОДРУК", "ПЕРЕДРУК", "ДЖЕРЕЛО_ID") VALUES (:prg, :srg, :firstprint, :reprint, :source_id)'),
            params,
        )
        return _last_insert_id(conn)


def link_wordform_quote(wordform_id: int, quote_id: int) -> None:
    sql = _insert_ignore_sql("СЛОВОФОРМА-ЦИТАТА", ["СЛОВОФОРМА_ID", "ЦИТАТА_ID"], ["СЛОВОФОРМА_ID", "ЦИТАТА_ID"])
    with ENGINE.begin() as conn:
        conn.execute(text(sql), {"СЛОВОФОРМА_ID": int(wordform_id), "ЦИТАТА_ID": int(quote_id)})


def link_definition_quote(definition_id: int, quote_id: int) -> None:
    sql = _insert_ignore_sql("ТЛУМАЧЕННЯ-ЦИТАТА", ["ТЛУМАЧЕННЯ_ID", "ЦИТАТА_ID"], ["ТЛУМАЧЕННЯ_ID", "ЦИТАТА_ID"])
    with ENGINE.begin() as conn:
        conn.execute(text(sql), {"ТЛУМАЧЕННЯ_ID": int(definition_id), "ЦИТАТА_ID": int(quote_id)})


def link_collocation_quote(collocation_id: int, quote_id: int) -> None:
    sql = _insert_ignore_sql("СТІЙКА СПОЛУКА-ЦИТАТА", ["СТІЙКА_СПОЛУКА_ID", "ЦИТАТА_ID"], ["СТІЙКА_СПОЛУКА_ID", "ЦИТАТА_ID"])
    with ENGINE.begin() as conn:
        conn.execute(text(sql), {"СТІЙКА_СПОЛУКА_ID": int(collocation_id), "ЦИТАТА_ID": int(quote_id)})


def insert_definition(word_id: int, text_value: str, reference_id: int | None, frequency: int, stylistics: str) -> int:
    params = {
        "text_value": text_value.strip(),
        "reference_id": reference_id,
        "frequency": int(frequency or 0),
        "word_id": int(word_id),
        "stylistics": stylistics,
    }
    with ENGINE.begin() as conn:
        if IS_POSTGRES:
            out_id = _scalar(
                conn,
                'INSERT INTO "ТЛУМАЧЕННЯ"("ТЛУМАЧЕННЯ", "ПОКЛИКАННЯ_ID", "ЧАСТОТА", "СЛОВО_ID", "СТИЛІСТИКА") '
                'VALUES (:text_value, :reference_id, :frequency, :word_id, :stylistics) RETURNING "ID"',
                params,
            )
            return int(out_id)
        conn.execute(
            text('INSERT INTO "ТЛУМАЧЕННЯ"("ТЛУМАЧЕННЯ", "ПОКЛИКАННЯ_ID", "ЧАСТОТА", "СЛОВО_ID", "СТИЛІСТИКА") '
                 'VALUES (:text_value, :reference_id, :frequency, :word_id, :stylistics)'),
            params,
        )
        return _last_insert_id(conn)


def insert_collocation(word_id: int, unit: str, unit_type: str, definition: str, frequency: int, stylistics: str) -> int:
    params = {
        "word_id": int(word_id),
        "unit": unit.strip(),
        "unit_type": unit_type,
        "definition": definition.strip(),
        "frequency": int(frequency or 0),
        "stylistics": stylistics,
    }
    with ENGINE.begin() as conn:
        if IS_POSTGRES:
            out_id = _scalar(
                conn,
                'INSERT INTO "СТІЙКА СПОЛУКА"("СЛОВО_ID", "ОДИНИЦЯ", "ТИП", "ТЛУМАЧЕННЯ", "ЧАСТОТА", "СТИЛІСТИКА") '
                'VALUES (:word_id, :unit, :unit_type, :definition, :frequency, :stylistics) RETURNING "ID"',
                params,
            )
            return int(out_id)
        conn.execute(
            text('INSERT INTO "СТІЙКА СПОЛУКА"("СЛОВО_ID", "ОДИНИЦЯ", "ТИП", "ТЛУМАЧЕННЯ", "ЧАСТОТА", "СТИЛІСТИКА") '
                 'VALUES (:word_id, :unit, :unit_type, :definition, :frequency, :stylistics)'),
            params,
        )
        return _last_insert_id(conn)


def get_dictionary_article(word_id: int) -> dict[str, list[dict] | dict | None]:
    with ENGINE.begin() as conn:
        word = _fetchone(conn, 'SELECT * FROM "СЛОВО" WHERE "ID" = :word_id', {"word_id": int(word_id)})
        definitions = _fetchall(
            conn,
            'SELECT t.*, p."СКОРОЧЕННЯ" AS "ПОКЛИКАННЯ_СКОРОЧЕННЯ", p."ПОВНА НАЗВА" AS "ПОКЛИКАННЯ_ПОВНА НАЗВА" '
            'FROM "ТЛУМАЧЕННЯ" t LEFT JOIN "ПОКЛИКАННЯ" p ON p."ID" = t."ПОКЛИКАННЯ_ID" '
            'WHERE t."СЛОВО_ID" = :word_id ORDER BY t."ID"',
            {"word_id": int(word_id)},
        )
        wordforms = _fetchall(conn, 'SELECT * FROM "СЛОВОФОРМА" WHERE "СЛОВО_ID" = :word_id ORDER BY "СЛОВОФОРМА"', {"word_id": int(word_id)})
        collocations = _fetchall(conn, 'SELECT * FROM "СТІЙКА СПОЛУКА" WHERE "СЛОВО_ID" = :word_id ORDER BY "ОДИНИЦЯ"', {"word_id": int(word_id)})
        quotes = _fetchall(
            conn,
            'SELECT DISTINCT c.*, d."СКОРОЧЕННЯ" AS "ДЖЕРЕЛО_СКОРОЧЕННЯ", d."ПОВНА НАЗВА" AS "ДЖЕРЕЛО_ПОВНА НАЗВА" '
            'FROM "ЦИТАТА" c '
            'LEFT JOIN "ДЖЕРЕЛО" d ON d."ID" = c."ДЖЕРЕЛО_ID" '
            'LEFT JOIN "СЛОВОФОРМА-ЦИТАТА" sc ON sc."ЦИТАТА_ID" = c."ID" '
            'LEFT JOIN "СЛОВОФОРМА" sf ON sf."ID" = sc."СЛОВОФОРМА_ID" '
            'WHERE sf."СЛОВО_ID" = :word_id ORDER BY c."ID" DESC',
            {"word_id": int(word_id)},
        )
    return {
        "word": word,
        "definitions": definitions,
        "wordforms": wordforms,
        "collocations": collocations,
        "quotes": quotes,
    }


def get_definition_quotes(definition_id: int) -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(
            conn,
            'SELECT c.*, d."СКОРОЧЕННЯ" AS "ДЖЕРЕЛО_СКОРОЧЕННЯ", d."ПОВНА НАЗВА" AS "ДЖЕРЕЛО_ПОВНА НАЗВА" '
            'FROM "ЦИТАТА" c '
            'LEFT JOIN "ДЖЕРЕЛО" d ON d."ID" = c."ДЖЕРЕЛО_ID" '
            'JOIN "ТЛУМАЧЕННЯ-ЦИТАТА" tc ON tc."ЦИТАТА_ID" = c."ID" '
            'WHERE tc."ТЛУМАЧЕННЯ_ID" = :definition_id ORDER BY c."ID"',
            {"definition_id": int(definition_id)},
        )


def get_collocation_quotes(collocation_id: int) -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(
            conn,
            'SELECT c.*, d."СКОРОЧЕННЯ" AS "ДЖЕРЕЛО_СКОРОЧЕННЯ", d."ПОВНА НАЗВА" AS "ДЖЕРЕЛО_ПОВНА НАЗВА" '
            'FROM "ЦИТАТА" c '
            'LEFT JOIN "ДЖЕРЕЛО" d ON d."ID" = c."ДЖЕРЕЛО_ID" '
            'JOIN "СТІЙКА СПОЛУКА-ЦИТАТА" sc ON sc."ЦИТАТА_ID" = c."ID" '
            'WHERE sc."СТІЙКА_СПОЛУКА_ID" = :collocation_id ORDER BY c."ID"',
            {"collocation_id": int(collocation_id)},
        )
