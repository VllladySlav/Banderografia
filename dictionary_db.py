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
        # Для локальної роботи із Supabase важливо не відкривати нове TCP-з’єднання
        # після кожної дії в інтерфейсі. Невеликий пул SQLAlchemy повторно
        # використовує вже відкрите з’єднання й помітно зменшує затримки.
        return create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_size=3,
            max_overflow=2,
        )

    return create_engine(url, future=True, pool_pre_ping=True)


ENGINE: Engine = _make_engine()
IS_POSTGRES = ENGINE.url.get_backend_name().startswith("postgresql")
IS_SQLITE = ENGINE.url.get_backend_name().startswith("sqlite")

_LEX_DB_INITIALIZED = False


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
    global _LEX_DB_INITIALIZED
    if _LEX_DB_INITIALIZED:
        return

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

        _ensure_extended_lex_schema(conn, id_type)

        for field_name, values in DEFAULT_OPTIONS.items():
            for value in values:
                add_option(field_name, value, conn=conn)

    _LEX_DB_INITIALIZED = True


def _table_columns(conn: Connection, table_name: str) -> set[str]:
    """Повертає назви колонок для поточної таблиці незалежно від backend."""
    if IS_POSTGRES:
        rows = conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = :table_name
                """
            ),
            {"table_name": table_name},
        ).fetchall()
        return {str(row[0]) for row in rows}
    rows = conn.execute(text(f'PRAGMA table_info("{table_name}")')).fetchall()
    return {str(row[1]) for row in rows}


def _add_column_if_missing(conn: Connection, table_name: str, column_name: str, column_sql_type: str) -> None:
    if column_name in _table_columns(conn, table_name):
        return
    conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_sql_type}'))


def _ensure_extended_lex_schema(conn: Connection, id_type: str) -> None:
    """Міграції для повної моделі словникової статті."""
    _add_column_if_missing(conn, "СЛОВО", "НАГОЛОШЕНА ФОРМА", "TEXT")

    statements = [
        f"""
        CREATE TABLE IF NOT EXISTS "ВАРІЯНТ" (
            "ID" {id_type},
            "СЛОВО_ID" INTEGER NOT NULL,
            "ВАРІЯНТ" TEXT NOT NULL,
            "НАГОЛОШЕНА ФОРМА" TEXT,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "ТИП" TEXT,
            "КОМЕНТАР" TEXT,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS "ВІДСИЛАННЯ" (
            "ID" {id_type},
            "СЛОВО_ID" INTEGER NOT NULL,
            "ТИП" TEXT,
            "ТЕКСТ" TEXT NOT NULL,
            "ЦІЛЬОВЕ_СЛОВО_ID" INTEGER,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE,
            FOREIGN KEY("ЦІЛЬОВЕ_СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE SET NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS "СЕМАНТИЧНИЙ ЗВ'ЯЗОК" (
            "ID" {id_type},
            "СЛОВО_ID" INTEGER NOT NULL,
            "ТИП" TEXT NOT NULL,
            "ОДИНИЦЯ" TEXT NOT NULL,
            "ПОВЯЗАНЕ_СЛОВО_ID" INTEGER,
            "КОМЕНТАР" TEXT,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE,
            FOREIGN KEY("ПОВЯЗАНЕ_СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE SET NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS "ПІДЗНАЧЕННЯ" (
            "ID" {id_type},
            "ТЛУМАЧЕННЯ_ID" INTEGER NOT NULL,
            "ТЕКСТ" TEXT NOT NULL,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "СТИЛІСТИКА" TEXT,
            FOREIGN KEY("ТЛУМАЧЕННЯ_ID") REFERENCES "ТЛУМАЧЕННЯ"("ID") ON DELETE CASCADE
        )
        """,
        'CREATE INDEX IF NOT EXISTS "idx_варіянт_слово" ON "ВАРІЯНТ"("СЛОВО_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_відсилання_слово" ON "ВІДСИЛАННЯ"("СЛОВО_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_семзв_слово" ON "СЕМАНТИЧНИЙ ЗВ\'ЯЗОК"("СЛОВО_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_підзначення_тлумачення" ON "ПІДЗНАЧЕННЯ"("ТЛУМАЧЕННЯ_ID")',
    ]
    _run_schema_statements(conn, statements)

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


def delete_user(user_id: int) -> None:
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "AUTH_USERS" WHERE "ID" = :user_id'), {"user_id": int(user_id)})


def ensure_default_admin_user() -> int:
    """Гарантує наявність єдиного вбудованого адміністратора вкладки «Словник»."""
    admin_name = "Владислав Кривенок"
    admin_email = "kryvenokvladyslav@gmail.com"
    admin_password = "vladyk2004"
    salt, hashed = hash_password(admin_password)
    params = {
        "name": admin_name,
        "email": admin_email.lower(),
        "salt": salt,
        "hashed": hashed,
        "role": "admin",
        "can_edit": 1,
    }
    with ENGINE.begin() as conn:
        row = _fetchone(conn, 'SELECT "ID" FROM "AUTH_USERS" WHERE lower("EMAIL") = lower(:email)', {"email": admin_email})
        if row:
            user_id = int(row["ID"])
            conn.execute(
                text('UPDATE "AUTH_USERS" SET "NAME"=:name, "PASSWORD_SALT"=:salt, "PASSWORD_HASH"=:hashed, "ROLE"=:role, "CAN_EDIT"=:can_edit WHERE "ID"=:user_id'),
                {**params, "user_id": user_id},
            )
            conn.execute(
                text("UPDATE \"AUTH_USERS\" SET \"ROLE\" = 'viewer', \"CAN_EDIT\" = 0 WHERE lower(\"EMAIL\") <> lower(:email) AND \"ROLE\" = 'admin'"),
                {"email": admin_email},
            )
            return user_id
        if IS_POSTGRES:
            user_id = int(_scalar(
                conn,
                'INSERT INTO "AUTH_USERS"("NAME", "EMAIL", "PASSWORD_SALT", "PASSWORD_HASH", "ROLE", "CAN_EDIT") '
                'VALUES (:name, :email, :salt, :hashed, :role, :can_edit) RETURNING "ID"',
                params,
            ))
            conn.execute(
                text("UPDATE \"AUTH_USERS\" SET \"ROLE\" = 'viewer', \"CAN_EDIT\" = 0 WHERE lower(\"EMAIL\") <> lower(:email) AND \"ROLE\" = 'admin'"),
                {"email": admin_email},
            )
            return user_id
        conn.execute(
            text('INSERT INTO "AUTH_USERS"("NAME", "EMAIL", "PASSWORD_SALT", "PASSWORD_HASH", "ROLE", "CAN_EDIT") '
                 'VALUES (:name, :email, :salt, :hashed, :role, :can_edit)'),
            params,
        )
        user_id = _last_insert_id(conn)
        conn.execute(
            text("UPDATE \"AUTH_USERS\" SET \"ROLE\" = 'viewer', \"CAN_EDIT\" = 0 WHERE lower(\"EMAIL\") <> lower(:email) AND \"ROLE\" = 'admin'"),
            {"email": admin_email},
        )
        return user_id


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


def delete_option(field_name: str, value: str, conn: Connection | None = None) -> None:
    """Видаляє значення зі службового випадного списку.

    Вилучення не змінює вже збережені словникові статті: воно тільки
    прибирає значення з таблиці DICTIONARY_OPTIONS для майбутнього вибору.
    """
    field_name = str(field_name or "").strip()
    value = str(value or "").strip()
    if not field_name or not value:
        return

    sql = 'DELETE FROM "DICTIONARY_OPTIONS" WHERE "FIELD_NAME" = :field_name AND "VALUE" = :value'
    params = {"field_name": field_name, "value": value}
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


# =========================
# Розширені функції для повної словникової статті
# =========================


def save_word_full(
    word_id: int | None,
    register: str,
    stressed_form: str,
    frequency: int,
    pos: str,
    grammar: str,
    stylistics: str,
    origin: str,
) -> int:
    """Створює / оновлює лему з наголошеною формою."""
    word_id_out = save_word(word_id, register, frequency, pos, grammar, stylistics, origin)
    with ENGINE.begin() as conn:
        conn.execute(
            text('UPDATE "СЛОВО" SET "НАГОЛОШЕНА ФОРМА" = :stressed WHERE "ID" = :word_id'),
            {"stressed": str(stressed_form or "").strip(), "word_id": int(word_id_out)},
        )
    return int(word_id_out)


def list_references(table: str, limit: int = 10000) -> list[dict]:
    if table not in {"ДЖЕРЕЛО", "ПОКЛИКАННЯ"}:
        raise ValueError("table must be ДЖЕРЕЛО or ПОКЛИКАННЯ")
    with ENGINE.begin() as conn:
        return _fetchall(conn, f'SELECT * FROM "{table}" ORDER BY "СКОРОЧЕННЯ", "ПОВНА НАЗВА" LIMIT :limit', {"limit": int(limit)})


def update_reference(table: str, item_id: int, abbr: str, full_title: str) -> None:
    if table not in {"ДЖЕРЕЛО", "ПОКЛИКАННЯ"}:
        raise ValueError("table must be ДЖЕРЕЛО or ПОКЛИКАННЯ")
    with ENGINE.begin() as conn:
        conn.execute(
            text(f'UPDATE "{table}" SET "СКОРОЧЕННЯ" = :abbr, "ПОВНА НАЗВА" = :full_title WHERE "ID" = :item_id'),
            {"abbr": str(abbr or "").strip(), "full_title": str(full_title or "").strip(), "item_id": int(item_id)},
        )


def list_wordforms_for_word(word_id: int) -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(conn, 'SELECT * FROM "СЛОВОФОРМА" WHERE "СЛОВО_ID" = :word_id ORDER BY "СЛОВОФОРМА"', {"word_id": int(word_id)})


def get_wordform_by_id(wordform_id: int) -> dict | None:
    with ENGINE.begin() as conn:
        return _fetchone(conn, 'SELECT * FROM "СЛОВОФОРМА" WHERE "ID" = :id', {"id": int(wordform_id)})


def delete_wordform(wordform_id: int) -> None:
    word_id = None
    with ENGINE.begin() as conn:
        row = _fetchone(conn, 'SELECT "СЛОВО_ID" FROM "СЛОВОФОРМА" WHERE "ID" = :id', {"id": int(wordform_id)})
        if row:
            word_id = int(row["СЛОВО_ID"])
        conn.execute(text('DELETE FROM "СЛОВОФОРМА" WHERE "ID" = :id'), {"id": int(wordform_id)})
    if word_id:
        recompute_word_frequency(word_id)


def list_definitions(word_id: int) -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(
            conn,
            'SELECT t.*, p."СКОРОЧЕННЯ" AS "ПОКЛИКАННЯ_СКОРОЧЕННЯ", p."ПОВНА НАЗВА" AS "ПОКЛИКАННЯ_ПОВНА НАЗВА" '
            'FROM "ТЛУМАЧЕННЯ" t LEFT JOIN "ПОКЛИКАННЯ" p ON p."ID" = t."ПОКЛИКАННЯ_ID" '
            'WHERE t."СЛОВО_ID" = :word_id ORDER BY t."ID"',
            {"word_id": int(word_id)},
        )


def update_definition(definition_id: int, text_value: str, reference_id: int | None, frequency: int, stylistics: str) -> None:
    with ENGINE.begin() as conn:
        conn.execute(
            text('UPDATE "ТЛУМАЧЕННЯ" SET "ТЛУМАЧЕННЯ"=:text_value, "ПОКЛИКАННЯ_ID"=:reference_id, "ЧАСТОТА"=:frequency, "СТИЛІСТИКА"=:stylistics WHERE "ID"=:definition_id'),
            {"definition_id": int(definition_id), "text_value": str(text_value or "").strip(), "reference_id": reference_id, "frequency": int(frequency or 0), "stylistics": stylistics},
        )


def delete_definition(definition_id: int) -> None:
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "ТЛУМАЧЕННЯ" WHERE "ID" = :id'), {"id": int(definition_id)})


def upsert_variant(variant_id: int | None, word_id: int, variant: str, stressed_form: str, frequency: int, variant_type: str, comment: str) -> int:
    params = {"id": int(variant_id or 0), "word_id": int(word_id), "variant": str(variant or "").strip(), "stressed": str(stressed_form or "").strip(), "frequency": int(frequency or 0), "variant_type": variant_type, "comment": comment}
    if not params["variant"]:
        return 0
    with ENGINE.begin() as conn:
        if variant_id:
            conn.execute(text('UPDATE "ВАРІЯНТ" SET "ВАРІЯНТ"=:variant, "НАГОЛОШЕНА ФОРМА"=:stressed, "ЧАСТОТА"=:frequency, "ТИП"=:variant_type, "КОМЕНТАР"=:comment WHERE "ID"=:id'), params)
            return int(variant_id)
        if IS_POSTGRES:
            return int(_scalar(conn, 'INSERT INTO "ВАРІЯНТ"("СЛОВО_ID", "ВАРІЯНТ", "НАГОЛОШЕНА ФОРМА", "ЧАСТОТА", "ТИП", "КОМЕНТАР") VALUES (:word_id, :variant, :stressed, :frequency, :variant_type, :comment) RETURNING "ID"', params))
        conn.execute(text('INSERT INTO "ВАРІЯНТ"("СЛОВО_ID", "ВАРІЯНТ", "НАГОЛОШЕНА ФОРМА", "ЧАСТОТА", "ТИП", "КОМЕНТАР") VALUES (:word_id, :variant, :stressed, :frequency, :variant_type, :comment)'), params)
        return _last_insert_id(conn)


def list_variants(word_id: int) -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(conn, 'SELECT * FROM "ВАРІЯНТ" WHERE "СЛОВО_ID" = :word_id ORDER BY "ID"', {"word_id": int(word_id)})


def delete_variant(variant_id: int) -> None:
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "ВАРІЯНТ" WHERE "ID"=:id'), {"id": int(variant_id)})


def upsert_crossref(ref_id: int | None, word_id: int, ref_type: str, text_value: str, target_word_id: int | None = None) -> int:
    params = {"id": int(ref_id or 0), "word_id": int(word_id), "ref_type": ref_type, "text_value": str(text_value or "").strip(), "target_word_id": int(target_word_id) if target_word_id else None}
    if not params["text_value"]:
        return 0
    with ENGINE.begin() as conn:
        if ref_id:
            conn.execute(text('UPDATE "ВІДСИЛАННЯ" SET "ТИП"=:ref_type, "ТЕКСТ"=:text_value, "ЦІЛЬОВЕ_СЛОВО_ID"=:target_word_id WHERE "ID"=:id'), params)
            return int(ref_id)
        if IS_POSTGRES:
            return int(_scalar(conn, 'INSERT INTO "ВІДСИЛАННЯ"("СЛОВО_ID", "ТИП", "ТЕКСТ", "ЦІЛЬОВЕ_СЛОВО_ID") VALUES (:word_id, :ref_type, :text_value, :target_word_id) RETURNING "ID"', params))
        conn.execute(text('INSERT INTO "ВІДСИЛАННЯ"("СЛОВО_ID", "ТИП", "ТЕКСТ", "ЦІЛЬОВЕ_СЛОВО_ID") VALUES (:word_id, :ref_type, :text_value, :target_word_id)'), params)
        return _last_insert_id(conn)


def list_crossrefs(word_id: int) -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(conn, 'SELECT * FROM "ВІДСИЛАННЯ" WHERE "СЛОВО_ID" = :word_id ORDER BY "ID"', {"word_id": int(word_id)})


def delete_crossref(ref_id: int) -> None:
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "ВІДСИЛАННЯ" WHERE "ID"=:id'), {"id": int(ref_id)})


def upsert_semantic_relation(rel_id: int | None, word_id: int, rel_type: str, unit: str, related_word_id: int | None = None, comment: str = "") -> int:
    params = {"id": int(rel_id or 0), "word_id": int(word_id), "rel_type": rel_type, "unit": str(unit or "").strip(), "related_word_id": int(related_word_id) if related_word_id else None, "comment": comment}
    if not params["unit"]:
        return 0
    with ENGINE.begin() as conn:
        if rel_id:
            conn.execute(text('UPDATE "СЕМАНТИЧНИЙ ЗВ\'ЯЗОК" SET "ТИП"=:rel_type, "ОДИНИЦЯ"=:unit, "ПОВЯЗАНЕ_СЛОВО_ID"=:related_word_id, "КОМЕНТАР"=:comment WHERE "ID"=:id'), params)
            return int(rel_id)
        if IS_POSTGRES:
            return int(_scalar(conn, 'INSERT INTO "СЕМАНТИЧНИЙ ЗВ\'ЯЗОК"("СЛОВО_ID", "ТИП", "ОДИНИЦЯ", "ПОВЯЗАНЕ_СЛОВО_ID", "КОМЕНТАР") VALUES (:word_id, :rel_type, :unit, :related_word_id, :comment) RETURNING "ID"', params))
        conn.execute(text('INSERT INTO "СЕМАНТИЧНИЙ ЗВ\'ЯЗОК"("СЛОВО_ID", "ТИП", "ОДИНИЦЯ", "ПОВЯЗАНЕ_СЛОВО_ID", "КОМЕНТАР") VALUES (:word_id, :rel_type, :unit, :related_word_id, :comment)'), params)
        return _last_insert_id(conn)


def list_semantic_relations(word_id: int) -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(conn, 'SELECT * FROM "СЕМАНТИЧНИЙ ЗВ\'ЯЗОК" WHERE "СЛОВО_ID" = :word_id ORDER BY "ТИП", "ОДИНИЦЯ"', {"word_id": int(word_id)})


def delete_semantic_relation(rel_id: int) -> None:
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "СЕМАНТИЧНИЙ ЗВ\'ЯЗОК" WHERE "ID"=:id'), {"id": int(rel_id)})


def upsert_subdefinition(sub_id: int | None, definition_id: int, text_value: str, frequency: int, stylistics: str) -> int:
    params = {"id": int(sub_id or 0), "definition_id": int(definition_id), "text_value": str(text_value or "").strip(), "frequency": int(frequency or 0), "stylistics": stylistics}
    if not params["text_value"]:
        return 0
    with ENGINE.begin() as conn:
        if sub_id:
            conn.execute(text('UPDATE "ПІДЗНАЧЕННЯ" SET "ТЕКСТ"=:text_value, "ЧАСТОТА"=:frequency, "СТИЛІСТИКА"=:stylistics WHERE "ID"=:id'), params)
            return int(sub_id)
        if IS_POSTGRES:
            return int(_scalar(conn, 'INSERT INTO "ПІДЗНАЧЕННЯ"("ТЛУМАЧЕННЯ_ID", "ТЕКСТ", "ЧАСТОТА", "СТИЛІСТИКА") VALUES (:definition_id, :text_value, :frequency, :stylistics) RETURNING "ID"', params))
        conn.execute(text('INSERT INTO "ПІДЗНАЧЕННЯ"("ТЛУМАЧЕННЯ_ID", "ТЕКСТ", "ЧАСТОТА", "СТИЛІСТИКА") VALUES (:definition_id, :text_value, :frequency, :stylistics)'), params)
        return _last_insert_id(conn)


def list_subdefinitions(definition_id: int) -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(conn, 'SELECT * FROM "ПІДЗНАЧЕННЯ" WHERE "ТЛУМАЧЕННЯ_ID" = :definition_id ORDER BY "ID"', {"definition_id": int(definition_id)})


def delete_subdefinition(sub_id: int) -> None:
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "ПІДЗНАЧЕННЯ" WHERE "ID"=:id'), {"id": int(sub_id)})


def update_quote(quote_id: int, prg: int | None, srg: int | None, firstprint: str, reprint: str, source_id: int | None) -> None:
    with ENGINE.begin() as conn:
        conn.execute(
            text('UPDATE "ЦИТАТА" SET "PRG"=:prg, "SRG"=:srg, "ПЕРШОДРУК"=:firstprint, "ПЕРЕДРУК"=:reprint, "ДЖЕРЕЛО_ID"=:source_id WHERE "ID"=:quote_id'),
            {"quote_id": int(quote_id), "prg": prg, "srg": srg, "firstprint": str(firstprint or "").strip(), "reprint": str(reprint or "").strip(), "source_id": source_id},
        )


def delete_quote(quote_id: int) -> None:
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "ЦИТАТА" WHERE "ID"=:id'), {"id": int(quote_id)})


def unlink_definition_quote(definition_id: int, quote_id: int) -> None:
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "ТЛУМАЧЕННЯ-ЦИТАТА" WHERE "ТЛУМАЧЕННЯ_ID"=:definition_id AND "ЦИТАТА_ID"=:quote_id'), {"definition_id": int(definition_id), "quote_id": int(quote_id)})


def unlink_collocation_quote(collocation_id: int, quote_id: int) -> None:
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "СТІЙКА СПОЛУКА-ЦИТАТА" WHERE "СТІЙКА_СПОЛУКА_ID"=:collocation_id AND "ЦИТАТА_ID"=:quote_id'), {"collocation_id": int(collocation_id), "quote_id": int(quote_id)})


def unlink_wordform_quote(wordform_id: int, quote_id: int) -> None:
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "СЛОВОФОРМА-ЦИТАТА" WHERE "СЛОВОФОРМА_ID"=:wordform_id AND "ЦИТАТА_ID"=:quote_id'), {"wordform_id": int(wordform_id), "quote_id": int(quote_id)})


def list_collocations(word_id: int) -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(conn, 'SELECT * FROM "СТІЙКА СПОЛУКА" WHERE "СЛОВО_ID" = :word_id ORDER BY "ОДИНИЦЯ"', {"word_id": int(word_id)})


def update_collocation(collocation_id: int, unit: str, unit_type: str, definition: str, frequency: int, stylistics: str) -> None:
    with ENGINE.begin() as conn:
        conn.execute(
            text('UPDATE "СТІЙКА СПОЛУКА" SET "ОДИНИЦЯ"=:unit, "ТИП"=:unit_type, "ТЛУМАЧЕННЯ"=:definition, "ЧАСТОТА"=:frequency, "СТИЛІСТИКА"=:stylistics WHERE "ID"=:id'),
            {"id": int(collocation_id), "unit": str(unit or "").strip(), "unit_type": unit_type, "definition": str(definition or "").strip(), "frequency": int(frequency or 0), "stylistics": stylistics},
        )


def delete_collocation(collocation_id: int) -> None:
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "СТІЙКА СПОЛУКА" WHERE "ID"=:id'), {"id": int(collocation_id)})


def get_wordform_quotes(wordform_id: int) -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(
            conn,
            'SELECT c.*, d."СКОРОЧЕННЯ" AS "ДЖЕРЕЛО_СКОРОЧЕННЯ", d."ПОВНА НАЗВА" AS "ДЖЕРЕЛО_ПОВНА НАЗВА" '
            'FROM "ЦИТАТА" c LEFT JOIN "ДЖЕРЕЛО" d ON d."ID" = c."ДЖЕРЕЛО_ID" '
            'JOIN "СЛОВОФОРМА-ЦИТАТА" wc ON wc."ЦИТАТА_ID" = c."ID" '
            'WHERE wc."СЛОВОФОРМА_ID" = :wordform_id ORDER BY c."ID"',
            {"wordform_id": int(wordform_id)},
        )


def get_dictionary_article_full(word_id: int) -> dict[str, Any]:
    article = get_dictionary_article(word_id)
    definitions = article.get("definitions") or []
    for definition in definitions:
        definition["subdefinitions"] = list_subdefinitions(int(definition["ID"]))
        definition["quotes"] = get_definition_quotes(int(definition["ID"]))
    collocations = article.get("collocations") or []
    for collocation in collocations:
        collocation["quotes"] = get_collocation_quotes(int(collocation["ID"]))
    wordforms = article.get("wordforms") or []
    for wordform in wordforms:
        wordform["quotes"] = get_wordform_quotes(int(wordform["ID"]))
    article["definitions"] = definitions
    article["collocations"] = collocations
    article["wordforms"] = wordforms
    article["variants"] = list_variants(word_id)
    article["crossrefs"] = list_crossrefs(word_id)
    article["semantic_relations"] = list_semantic_relations(word_id)
    return article


# =========================
# Додаткові безпечні уточнення для розведення словоформ за значеннями,
# граматичними та стилістичними параметрами
# =========================


def _ensure_extended_lex_schema(conn: Connection, id_type: str) -> None:  # type: ignore[no-redef]
    """Міграції для повної моделі словникової статті.

    Функція перевизначає попередню версію й лишає всі уже створені таблиці,
    додаючи тільки ті поля й таблиці, яких бракує для нових зон редактора.
    """
    _add_column_if_missing(conn, "СЛОВО", "НАГОЛОШЕНА ФОРМА", "TEXT")
    _add_column_if_missing(conn, "СЛОВОФОРМА", "ЧАСТИНА МОВИ", "TEXT")
    _add_column_if_missing(conn, "СЛОВОФОРМА", "ГРАМАТИЧНІ ОЗНАКИ", "TEXT")
    _add_column_if_missing(conn, "СЛОВОФОРМА", "СТИЛІСТИКА", "TEXT")
    _add_column_if_missing(conn, "СЛОВОФОРМА", "ТЛУМАЧЕННЯ_ID", "INTEGER")

    statements = [
        f"""
        CREATE TABLE IF NOT EXISTS "ВАРІЯНТ" (
            "ID" {id_type},
            "СЛОВО_ID" INTEGER NOT NULL,
            "ВАРІЯНТ" TEXT NOT NULL,
            "НАГОЛОШЕНА ФОРМА" TEXT,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "ТИП" TEXT,
            "КОМЕНТАР" TEXT,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS "ВІДСИЛАННЯ" (
            "ID" {id_type},
            "СЛОВО_ID" INTEGER NOT NULL,
            "ТИП" TEXT,
            "ТЕКСТ" TEXT NOT NULL,
            "ЦІЛЬОВЕ_СЛОВО_ID" INTEGER,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE,
            FOREIGN KEY("ЦІЛЬОВЕ_СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE SET NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS "СЕМАНТИЧНИЙ ЗВ'ЯЗОК" (
            "ID" {id_type},
            "СЛОВО_ID" INTEGER NOT NULL,
            "ТИП" TEXT NOT NULL,
            "ОДИНИЦЯ" TEXT NOT NULL,
            "ПОВЯЗАНЕ_СЛОВО_ID" INTEGER,
            "КОМЕНТАР" TEXT,
            FOREIGN KEY("СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE CASCADE,
            FOREIGN KEY("ПОВЯЗАНЕ_СЛОВО_ID") REFERENCES "СЛОВО"("ID") ON DELETE SET NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS "ПІДЗНАЧЕННЯ" (
            "ID" {id_type},
            "ТЛУМАЧЕННЯ_ID" INTEGER NOT NULL,
            "ТЕКСТ" TEXT NOT NULL,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "СТИЛІСТИКА" TEXT,
            FOREIGN KEY("ТЛУМАЧЕННЯ_ID") REFERENCES "ТЛУМАЧЕННЯ"("ID") ON DELETE CASCADE
        )
        """,
        'CREATE INDEX IF NOT EXISTS "idx_варіянт_слово" ON "ВАРІЯНТ"("СЛОВО_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_відсилання_слово" ON "ВІДСИЛАННЯ"("СЛОВО_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_семзв_слово" ON "СЕМАНТИЧНИЙ ЗВ\'ЯЗОК"("СЛОВО_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_підзначення_тлумачення" ON "ПІДЗНАЧЕННЯ"("ТЛУМАЧЕННЯ_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_словоформа_значення" ON "СЛОВОФОРМА"("ТЛУМАЧЕННЯ_ID")',
    ]
    _run_schema_statements(conn, statements)


def recompute_definition_frequency(definition_id: int) -> int:
    """Перераховує частоту значення за кількістю прикріплених ілюстрацій."""
    with ENGINE.begin() as conn:
        total = int(_scalar(
            conn,
            'SELECT COUNT(DISTINCT "ЦИТАТА_ID") FROM "ТЛУМАЧЕННЯ-ЦИТАТА" WHERE "ТЛУМАЧЕННЯ_ID" = :definition_id',
            {"definition_id": int(definition_id)},
        ) or 0)
        conn.execute(
            text('UPDATE "ТЛУМАЧЕННЯ" SET "ЧАСТОТА" = :total WHERE "ID" = :definition_id'),
            {"total": total, "definition_id": int(definition_id)},
        )
    return total


def upsert_wordform(  # type: ignore[no-redef]
    word_id: int,
    wordform: str,
    frequency: int,
    pos: str = "",
    grammar: str = "",
    stylistics: str = "",
    definition_id: int | None = None,
) -> int:
    """Створює або оновлює словоформу.

    Однакова графічна словоформа може мати кілька окремих записів, якщо вона
    відрізняється значенням, граматичними ознаками або стилістичними ремарками.
    """
    wordform = str(wordform or "").strip()
    grammar = str(grammar or "").strip()
    stylistics = str(stylistics or "").strip()
    definition_id_norm = int(definition_id) if definition_id else None
    params = {
        "word_id": int(word_id),
        "wordform": wordform,
        "frequency": int(frequency or 0),
        "pos": str(pos or ""),
        "grammar": grammar,
        "stylistics": stylistics,
        "definition_id": definition_id_norm,
    }
    with ENGINE.begin() as conn:
        row = _fetchone(
            conn,
            'SELECT "ID" FROM "СЛОВОФОРМА" '
            'WHERE "СЛОВО_ID" = :word_id AND "СЛОВОФОРМА" = :wordform '
            'AND COALESCE("ГРАМАТИЧНІ ОЗНАКИ", \'\') = COALESCE(:grammar, \'\') '
            'AND COALESCE("СТИЛІСТИКА", \'\') = COALESCE(:stylistics, \'\') '
            'AND COALESCE("ТЛУМАЧЕННЯ_ID", -1) = COALESCE(:definition_id, -1) '
            'ORDER BY "ID" LIMIT 1',
            params,
        )
        if row:
            out_id = int(row["ID"])
            params["id"] = out_id
            conn.execute(
                text('UPDATE "СЛОВОФОРМА" SET "ЧАСТОТА" = :frequency, "ЧАСТИНА МОВИ" = :pos, "ГРАМАТИЧНІ ОЗНАКИ" = :grammar, "СТИЛІСТИКА" = :stylistics, "ТЛУМАЧЕННЯ_ID" = :definition_id WHERE "ID" = :id'),
                params,
            )
        else:
            if IS_POSTGRES:
                out_id = int(_scalar(
                    conn,
                    'INSERT INTO "СЛОВОФОРМА"("СЛОВОФОРМА", "ЧАСТОТА", "СЛОВО_ID", "ЧАСТИНА МОВИ", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА", "ТЛУМАЧЕННЯ_ID") '
                    'VALUES (:wordform, :frequency, :word_id, :pos, :grammar, :stylistics, :definition_id) RETURNING "ID"',
                    params,
                ))
            else:
                conn.execute(
                    text('INSERT INTO "СЛОВОФОРМА"("СЛОВОФОРМА", "ЧАСТОТА", "СЛОВО_ID", "ЧАСТИНА МОВИ", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА", "ТЛУМАЧЕННЯ_ID") '
                         'VALUES (:wordform, :frequency, :word_id, :pos, :grammar, :stylistics, :definition_id)'),
                    params,
                )
                out_id = _last_insert_id(conn)
    recompute_word_frequency(int(word_id))
    return int(out_id)


def update_wordform(wordform_id: int, wordform: str, frequency: int, pos: str = "", grammar: str = "", stylistics: str = "", definition_id: int | None = None) -> None:
    word_id = None
    with ENGINE.begin() as conn:
        old = _fetchone(conn, 'SELECT "СЛОВО_ID" FROM "СЛОВОФОРМА" WHERE "ID" = :id', {"id": int(wordform_id)})
        if old:
            word_id = int(old["СЛОВО_ID"])
        conn.execute(
            text('UPDATE "СЛОВОФОРМА" SET "СЛОВОФОРМА"=:wordform, "ЧАСТОТА"=:frequency, "ЧАСТИНА МОВИ"=:pos, "ГРАМАТИЧНІ ОЗНАКИ"=:grammar, "СТИЛІСТИКА"=:stylistics, "ТЛУМАЧЕННЯ_ID"=:definition_id WHERE "ID"=:id'),
            {
                "id": int(wordform_id),
                "wordform": str(wordform or "").strip(),
                "frequency": int(frequency or 0),
                "pos": str(pos or ""),
                "grammar": str(grammar or ""),
                "stylistics": str(stylistics or ""),
                "definition_id": int(definition_id) if definition_id else None,
            },
        )
    if word_id:
        recompute_word_frequency(word_id)


def get_wordform(word_id: int, wordform: str) -> dict | None:  # type: ignore[no-redef]
    with ENGINE.begin() as conn:
        return _fetchone(
            conn,
            'SELECT * FROM "СЛОВОФОРМА" WHERE "СЛОВО_ID" = :word_id AND "СЛОВОФОРМА" = :wordform ORDER BY "ID" LIMIT 1',
            {"word_id": int(word_id), "wordform": str(wordform or "")},
        )


def list_wordforms_for_word(word_id: int) -> list[dict]:  # type: ignore[no-redef]
    with ENGINE.begin() as conn:
        return _fetchall(
            conn,
            'SELECT sf.*, t."ТЛУМАЧЕННЯ" AS "ЗНАЧЕННЯ", t."ID" AS "ЗНАЧЕННЯ_ID" '
            'FROM "СЛОВОФОРМА" sf LEFT JOIN "ТЛУМАЧЕННЯ" t ON t."ID" = sf."ТЛУМАЧЕННЯ_ID" '
            'WHERE sf."СЛОВО_ID" = :word_id '
            'ORDER BY COALESCE(sf."ГРАМАТИЧНІ ОЗНАКИ", \'\'), sf."СЛОВОФОРМА", COALESCE(sf."ТЛУМАЧЕННЯ_ID", 0), sf."ID"',
            {"word_id": int(word_id)},
        )


def link_definition_quote(definition_id: int, quote_id: int) -> None:  # type: ignore[no-redef]
    sql = _insert_ignore_sql("ТЛУМАЧЕННЯ-ЦИТАТА", ["ТЛУМАЧЕННЯ_ID", "ЦИТАТА_ID"], ["ТЛУМАЧЕННЯ_ID", "ЦИТАТА_ID"])
    with ENGINE.begin() as conn:
        conn.execute(text(sql), {"ТЛУМАЧЕННЯ_ID": int(definition_id), "ЦИТАТА_ID": int(quote_id)})
    recompute_definition_frequency(int(definition_id))


def unlink_definition_quote(definition_id: int, quote_id: int) -> None:  # type: ignore[no-redef]
    with ENGINE.begin() as conn:
        conn.execute(text('DELETE FROM "ТЛУМАЧЕННЯ-ЦИТАТА" WHERE "ТЛУМАЧЕННЯ_ID"=:definition_id AND "ЦИТАТА_ID"=:quote_id'), {"definition_id": int(definition_id), "quote_id": int(quote_id)})
    recompute_definition_frequency(int(definition_id))


def delete_quote(quote_id: int) -> None:  # type: ignore[no-redef]
    affected_defs: list[int] = []
    with ENGINE.begin() as conn:
        affected_defs = [int(r["ТЛУМАЧЕННЯ_ID"]) for r in _fetchall(conn, 'SELECT "ТЛУМАЧЕННЯ_ID" FROM "ТЛУМАЧЕННЯ-ЦИТАТА" WHERE "ЦИТАТА_ID"=:id', {"id": int(quote_id)})]
        conn.execute(text('DELETE FROM "ЦИТАТА" WHERE "ID"=:id'), {"id": int(quote_id)})
    for did in affected_defs:
        recompute_definition_frequency(did)


def list_all_quotes_for_word(word_id: int) -> list[dict]:
    """Повертає всі цитати, прив’язані до словоформ, значень або сталих сполук поточної леми."""
    with ENGINE.begin() as conn:
        return _fetchall(
            conn,
            'SELECT DISTINCT c.*, d."СКОРОЧЕННЯ" AS "ДЖЕРЕЛО_СКОРОЧЕННЯ", d."ПОВНА НАЗВА" AS "ДЖЕРЕЛО_ПОВНА НАЗВА" '
            'FROM "ЦИТАТА" c '
            'LEFT JOIN "ДЖЕРЕЛО" d ON d."ID" = c."ДЖЕРЕЛО_ID" '
            'LEFT JOIN "СЛОВОФОРМА-ЦИТАТА" wq ON wq."ЦИТАТА_ID" = c."ID" '
            'LEFT JOIN "СЛОВОФОРМА" wf ON wf."ID" = wq."СЛОВОФОРМА_ID" '
            'LEFT JOIN "ТЛУМАЧЕННЯ-ЦИТАТА" dq ON dq."ЦИТАТА_ID" = c."ID" '
            'LEFT JOIN "ТЛУМАЧЕННЯ" def ON def."ID" = dq."ТЛУМАЧЕННЯ_ID" '
            'LEFT JOIN "СТІЙКА СПОЛУКА-ЦИТАТА" cq ON cq."ЦИТАТА_ID" = c."ID" '
            'LEFT JOIN "СТІЙКА СПОЛУКА" col ON col."ID" = cq."СТІЙКА_СПОЛУКА_ID" '
            'WHERE wf."СЛОВО_ID" = :word_id OR def."СЛОВО_ID" = :word_id OR col."СЛОВО_ID" = :word_id '
            'ORDER BY c."ID" DESC',
            {"word_id": int(word_id)},
        )


def get_definition_quote_ids(definition_id: int) -> set[int]:
    with ENGINE.begin() as conn:
        rows = _fetchall(conn, 'SELECT "ЦИТАТА_ID" FROM "ТЛУМАЧЕННЯ-ЦИТАТА" WHERE "ТЛУМАЧЕННЯ_ID"=:id', {"id": int(definition_id)})
    return {int(r["ЦИТАТА_ID"]) for r in rows}


def get_wordform_quote_ids(wordform_id: int) -> set[int]:
    with ENGINE.begin() as conn:
        rows = _fetchall(conn, 'SELECT "ЦИТАТА_ID" FROM "СЛОВОФОРМА-ЦИТАТА" WHERE "СЛОВОФОРМА_ID"=:id', {"id": int(wordform_id)})
    return {int(r["ЦИТАТА_ID"]) for r in rows}


def get_collocation_quote_ids(collocation_id: int) -> set[int]:
    with ENGINE.begin() as conn:
        rows = _fetchall(conn, 'SELECT "ЦИТАТА_ID" FROM "СТІЙКА СПОЛУКА-ЦИТАТА" WHERE "СТІЙКА_СПОЛУКА_ID"=:id', {"id": int(collocation_id)})
    return {int(r["ЦИТАТА_ID"]) for r in rows}


def get_dictionary_article_full(word_id: int) -> dict[str, Any]:  # type: ignore[no-redef]
    article = get_dictionary_article(word_id)
    definitions = article.get("definitions") or []
    for definition in definitions:
        # Частота значення є кількістю дібраних ілюстрацій.
        definition["ЧАСТОТА"] = recompute_definition_frequency(int(definition["ID"]))
        definition["subdefinitions"] = list_subdefinitions(int(definition["ID"]))
        definition["quotes"] = get_definition_quotes(int(definition["ID"]))
    collocations = article.get("collocations") or []
    for collocation in collocations:
        collocation["quotes"] = get_collocation_quotes(int(collocation["ID"]))
    wordforms = list_wordforms_for_word(word_id)
    for wordform in wordforms:
        wordform["quotes"] = get_wordform_quotes(int(wordform["ID"]))
    article["definitions"] = definitions
    article["collocations"] = collocations
    article["wordforms"] = wordforms
    article["variants"] = list_variants(word_id)
    article["crossrefs"] = list_crossrefs(word_id)
    article["semantic_relations"] = list_semantic_relations(word_id)
    article["all_quotes"] = list_all_quotes_for_word(word_id)
    return article


# =========================
# Порядок ілюстрацій у словниковій статті
# =========================

_LINK_TABLES: dict[str, tuple[str, str]] = {
    "definition": ('ТЛУМАЧЕННЯ-ЦИТАТА', 'ТЛУМАЧЕННЯ_ID'),
    "wordform": ('СЛОВОФОРМА-ЦИТАТА', 'СЛОВОФОРМА_ID'),
    "collocation": ('СТІЙКА СПОЛУКА-ЦИТАТА', 'СТІЙКА_СПОЛУКА_ID'),
}


def _ensure_quote_order_column(conn: Connection) -> None:
    for table_name, _owner_col in _LINK_TABLES.values():
        _add_column_if_missing(conn, table_name, "ПОРЯДОК", "INTEGER")


# Оновлена міграція: додає службову колонку ПОРЯДОК до проміжних таблиць,
# не змінюючи їхньої основної структури.
_PREV_ENSURE_EXTENDED_LEX_SCHEMA = _ensure_extended_lex_schema


def _ensure_extended_lex_schema(conn: Connection, id_type: str) -> None:  # type: ignore[no-redef]
    _PREV_ENSURE_EXTENDED_LEX_SCHEMA(conn, id_type)
    _ensure_quote_order_column(conn)


def _next_quote_order(conn: Connection, table_name: str, owner_col: str, owner_id: int) -> int:
    value = _scalar(
        conn,
        f'SELECT COALESCE(MAX("ПОРЯДОК"), 0) + 1 FROM "{table_name}" WHERE "{owner_col}" = :owner_id',
        {"owner_id": int(owner_id)},
    )
    return int(value or 1)


def _link_quote_with_order(kind: str, owner_id: int, quote_id: int) -> None:
    if kind not in _LINK_TABLES:
        raise ValueError("Unknown quote link kind")
    table_name, owner_col = _LINK_TABLES[kind]
    with ENGINE.begin() as conn:
        _ensure_quote_order_column(conn)
        row = _fetchone(
            conn,
            f'SELECT "ID" FROM "{table_name}" WHERE "{owner_col}" = :owner_id AND "ЦИТАТА_ID" = :quote_id',
            {"owner_id": int(owner_id), "quote_id": int(quote_id)},
        )
        if row:
            return
        order_value = _next_quote_order(conn, table_name, owner_col, int(owner_id))
        conn.execute(
            text(f'INSERT INTO "{table_name}"("{owner_col}", "ЦИТАТА_ID", "ПОРЯДОК") VALUES (:owner_id, :quote_id, :order_value)'),
            {"owner_id": int(owner_id), "quote_id": int(quote_id), "order_value": order_value},
        )


def link_definition_quote(definition_id: int, quote_id: int) -> None:  # type: ignore[no-redef]
    _link_quote_with_order("definition", int(definition_id), int(quote_id))
    recompute_definition_frequency(int(definition_id))


def link_wordform_quote(wordform_id: int, quote_id: int) -> None:  # type: ignore[no-redef]
    _link_quote_with_order("wordform", int(wordform_id), int(quote_id))


def link_collocation_quote(collocation_id: int, quote_id: int) -> None:  # type: ignore[no-redef]
    _link_quote_with_order("collocation", int(collocation_id), int(quote_id))


def get_definition_quotes(definition_id: int) -> list[dict]:  # type: ignore[no-redef]
    with ENGINE.begin() as conn:
        _ensure_quote_order_column(conn)
        return _fetchall(
            conn,
            'SELECT c.*, d."СКОРОЧЕННЯ" AS "ДЖЕРЕЛО_СКОРОЧЕННЯ", d."ПОВНА НАЗВА" AS "ДЖЕРЕЛО_ПОВНА НАЗВА", '
            'tc."ПОРЯДОК" AS "ПОРЯДОК_ІЛЮСТРАЦІЇ" '
            'FROM "ЦИТАТА" c '
            'LEFT JOIN "ДЖЕРЕЛО" d ON d."ID" = c."ДЖЕРЕЛО_ID" '
            'JOIN "ТЛУМАЧЕННЯ-ЦИТАТА" tc ON tc."ЦИТАТА_ID" = c."ID" '
            'WHERE tc."ТЛУМАЧЕННЯ_ID" = :definition_id '
            'ORDER BY COALESCE(tc."ПОРЯДОК", tc."ID"), tc."ID", c."ID"',
            {"definition_id": int(definition_id)},
        )


def get_collocation_quotes(collocation_id: int) -> list[dict]:  # type: ignore[no-redef]
    with ENGINE.begin() as conn:
        _ensure_quote_order_column(conn)
        return _fetchall(
            conn,
            'SELECT c.*, d."СКОРОЧЕННЯ" AS "ДЖЕРЕЛО_СКОРОЧЕННЯ", d."ПОВНА НАЗВА" AS "ДЖЕРЕЛО_ПОВНА НАЗВА", '
            'sc."ПОРЯДОК" AS "ПОРЯДОК_ІЛЮСТРАЦІЇ" '
            'FROM "ЦИТАТА" c '
            'LEFT JOIN "ДЖЕРЕЛО" d ON d."ID" = c."ДЖЕРЕЛО_ID" '
            'JOIN "СТІЙКА СПОЛУКА-ЦИТАТА" sc ON sc."ЦИТАТА_ID" = c."ID" '
            'WHERE sc."СТІЙКА_СПОЛУКА_ID" = :collocation_id '
            'ORDER BY COALESCE(sc."ПОРЯДОК", sc."ID"), sc."ID", c."ID"',
            {"collocation_id": int(collocation_id)},
        )


def get_wordform_quotes(wordform_id: int) -> list[dict]:  # type: ignore[no-redef]
    with ENGINE.begin() as conn:
        _ensure_quote_order_column(conn)
        return _fetchall(
            conn,
            'SELECT c.*, d."СКОРОЧЕННЯ" AS "ДЖЕРЕЛО_СКОРОЧЕННЯ", d."ПОВНА НАЗВА" AS "ДЖЕРЕЛО_ПОВНА НАЗВА", '
            'wc."ПОРЯДОК" AS "ПОРЯДОК_ІЛЮСТРАЦІЇ" '
            'FROM "ЦИТАТА" c LEFT JOIN "ДЖЕРЕЛО" d ON d."ID" = c."ДЖЕРЕЛО_ID" '
            'JOIN "СЛОВОФОРМА-ЦИТАТА" wc ON wc."ЦИТАТА_ID" = c."ID" '
            'WHERE wc."СЛОВОФОРМА_ID" = :wordform_id '
            'ORDER BY COALESCE(wc."ПОРЯДОК", wc."ID"), wc."ID", c."ID"',
            {"wordform_id": int(wordform_id)},
        )


def _quote_link_rows(conn: Connection, kind: str, owner_id: int) -> list[dict]:
    if kind not in _LINK_TABLES:
        raise ValueError("Unknown quote link kind")
    table_name, owner_col = _LINK_TABLES[kind]
    _ensure_quote_order_column(conn)
    return _fetchall(
        conn,
        f'SELECT "ID", "ЦИТАТА_ID", COALESCE("ПОРЯДОК", "ID") AS "ORDER_KEY" FROM "{table_name}" WHERE "{owner_col}" = :owner_id ORDER BY COALESCE("ПОРЯДОК", "ID"), "ID"',
        {"owner_id": int(owner_id)},
    )


def normalize_quote_order(kind: str, owner_id: int) -> None:
    if kind not in _LINK_TABLES:
        raise ValueError("Unknown quote link kind")
    table_name, _owner_col = _LINK_TABLES[kind]
    with ENGINE.begin() as conn:
        rows = _quote_link_rows(conn, kind, int(owner_id))
        for pos, row in enumerate(rows, start=1):
            conn.execute(
                text(f'UPDATE "{table_name}" SET "ПОРЯДОК" = :pos WHERE "ID" = :link_id'),
                {"pos": pos, "link_id": int(row["ID"])},
            )


def move_quote_link(kind: str, owner_id: int, quote_id: int, direction: str) -> None:
    """Пересуває ілюстрацію вгору або вниз у межах конкретного значення / словоформи / сполуки."""
    if kind not in _LINK_TABLES:
        raise ValueError("Unknown quote link kind")
    table_name, _owner_col = _LINK_TABLES[kind]
    normalize_quote_order(kind, int(owner_id))
    with ENGINE.begin() as conn:
        rows = _quote_link_rows(conn, kind, int(owner_id))
        ids = [int(r["ЦИТАТА_ID"]) for r in rows]
        try:
            idx = ids.index(int(quote_id))
        except ValueError:
            return
        if direction == "up":
            other_idx = idx - 1
        elif direction == "down":
            other_idx = idx + 1
        else:
            return
        if other_idx < 0 or other_idx >= len(rows):
            return
        current = rows[idx]
        other = rows[other_idx]
        current_order = int(idx + 1)
        other_order = int(other_idx + 1)
        conn.execute(text(f'UPDATE "{table_name}" SET "ПОРЯДОК" = :order_value WHERE "ID" = :link_id'), {"order_value": other_order, "link_id": int(current["ID"])})
        conn.execute(text(f'UPDATE "{table_name}" SET "ПОРЯДОК" = :order_value WHERE "ID" = :link_id'), {"order_value": current_order, "link_id": int(other["ID"])})


def get_quote_attachments(quote_id: int) -> list[dict]:
    """Повертає всі місця, до яких прикріплена цитата, щоб можна було міняти її позицію."""
    with ENGINE.begin() as conn:
        _ensure_quote_order_column(conn)
        out: list[dict] = []
        out.extend(_fetchall(
            conn,
            'SELECT \'wordform\' AS "KIND", wc."СЛОВОФОРМА_ID" AS "OWNER_ID", wc."ПОРЯДОК" AS "ПОРЯДОК", '
            'wf."СЛОВОФОРМА" AS "LABEL" '
            'FROM "СЛОВОФОРМА-ЦИТАТА" wc JOIN "СЛОВОФОРМА" wf ON wf."ID" = wc."СЛОВОФОРМА_ID" '
            'WHERE wc."ЦИТАТА_ID" = :quote_id',
            {"quote_id": int(quote_id)},
        ))
        out.extend(_fetchall(
            conn,
            'SELECT \'definition\' AS "KIND", dc."ТЛУМАЧЕННЯ_ID" AS "OWNER_ID", dc."ПОРЯДОК" AS "ПОРЯДОК", '
            'substr(COALESCE(t."ТЛУМАЧЕННЯ", \'\'), 1, 90) AS "LABEL" '
            'FROM "ТЛУМАЧЕННЯ-ЦИТАТА" dc JOIN "ТЛУМАЧЕННЯ" t ON t."ID" = dc."ТЛУМАЧЕННЯ_ID" '
            'WHERE dc."ЦИТАТА_ID" = :quote_id',
            {"quote_id": int(quote_id)},
        ))
        out.extend(_fetchall(
            conn,
            'SELECT \'collocation\' AS "KIND", cc."СТІЙКА_СПОЛУКА_ID" AS "OWNER_ID", cc."ПОРЯДОК" AS "ПОРЯДОК", '
            'c."ОДИНИЦЯ" AS "LABEL" '
            'FROM "СТІЙКА СПОЛУКА-ЦИТАТА" cc JOIN "СТІЙКА СПОЛУКА" c ON c."ID" = cc."СТІЙКА_СПОЛУКА_ID" '
            'WHERE cc."ЦИТАТА_ID" = :quote_id',
            {"quote_id": int(quote_id)},
        ))
    return out


# Останнє перевизначення повної статті: використовує вже впорядковані цитати.
def get_dictionary_article_full(word_id: int) -> dict[str, Any]:  # type: ignore[no-redef]
    article = get_dictionary_article(word_id)
    definitions = article.get("definitions") or []
    for definition in definitions:
        definition["ЧАСТОТА"] = recompute_definition_frequency(int(definition["ID"]))
        definition["subdefinitions"] = list_subdefinitions(int(definition["ID"]))
        definition["quotes"] = get_definition_quotes(int(definition["ID"]))
    collocations = article.get("collocations") or []
    for collocation in collocations:
        collocation["quotes"] = get_collocation_quotes(int(collocation["ID"]))
    wordforms = list_wordforms_for_word(word_id)
    for wordform in wordforms:
        wordform["quotes"] = get_wordform_quotes(int(wordform["ID"]))
    article["definitions"] = definitions
    article["collocations"] = collocations
    article["wordforms"] = wordforms
    article["variants"] = list_variants(word_id)
    article["crossrefs"] = list_crossrefs(word_id)
    article["semantic_relations"] = list_semantic_relations(word_id)
    article["all_quotes"] = list_all_quotes_for_word(word_id)
    return article

# =========================
# Варіянтні словоформи й підсумкова частота варіянтів
# =========================

_PREV_ENSURE_EXTENDED_LEX_SCHEMA_VARIANT_WF = _ensure_extended_lex_schema


def _ensure_variant_wordform_schema(conn: Connection, id_type: str) -> None:
    """Додає таблицю для зведення словоформ варіянта до одного варіянта."""
    statements = [
        f'''
        CREATE TABLE IF NOT EXISTS "ВАРІЯНТ-СЛОВОФОРМА" (
            "ID" {id_type},
            "ВАРІЯНТ_ID" INTEGER NOT NULL,
            "СЛОВОФОРМА" TEXT NOT NULL,
            "ЧАСТОТА" INTEGER DEFAULT 0,
            "ГРАМАТИЧНІ ОЗНАКИ" TEXT,
            "СТИЛІСТИКА" TEXT,
            "ТЛУМАЧЕННЯ_ID" INTEGER,
            FOREIGN KEY("ВАРІЯНТ_ID") REFERENCES "ВАРІЯНТ"("ID") ON DELETE CASCADE,
            FOREIGN KEY("ТЛУМАЧЕННЯ_ID") REFERENCES "ТЛУМАЧЕННЯ"("ID") ON DELETE SET NULL
        )
        ''',
        'CREATE INDEX IF NOT EXISTS "idx_варіянт_словоформа_варіянт" ON "ВАРІЯНТ-СЛОВОФОРМА"("ВАРІЯНТ_ID")',
        'CREATE INDEX IF NOT EXISTS "idx_варіянт_словоформа_значення" ON "ВАРІЯНТ-СЛОВОФОРМА"("ТЛУМАЧЕННЯ_ID")',
    ]
    _run_schema_statements(conn, statements)


def _ensure_extended_lex_schema(conn: Connection, id_type: str) -> None:  # type: ignore[no-redef]
    _PREV_ENSURE_EXTENDED_LEX_SCHEMA_VARIANT_WF(conn, id_type)
    _ensure_variant_wordform_schema(conn, id_type)


def _base_wordform_frequency(conn: Connection, word_id: int) -> int:
    return int(_scalar(
        conn,
        'SELECT COALESCE(SUM("ЧАСТОТА"), 0) FROM "СЛОВОФОРМА" WHERE "СЛОВО_ID" = :word_id',
        {"word_id": int(word_id)},
    ) or 0)


def _variant_frequency_sum(conn: Connection, word_id: int) -> int:
    return int(_scalar(
        conn,
        'SELECT COALESCE(SUM("ЧАСТОТА"), 0) FROM "ВАРІЯНТ" WHERE "СЛОВО_ID" = :word_id',
        {"word_id": int(word_id)},
    ) or 0)


def recompute_word_frequency(word_id: int) -> int:  # type: ignore[no-redef]
    """Перераховує загальну частоту леми: основні словоформи + частоти варіянтів."""
    with ENGINE.begin() as conn:
        base_total = _base_wordform_frequency(conn, int(word_id))
        variant_total = _variant_frequency_sum(conn, int(word_id))
        total = int(base_total + variant_total)
        conn.execute(
            text('UPDATE "СЛОВО" SET "ЧАСТОТА" = :total WHERE "ID" = :word_id'),
            {"total": total, "word_id": int(word_id)},
        )
    return total


def recompute_variant_frequency(variant_id: int) -> int:
    """Перераховує частоту варіянта як суму прив’язаних до нього словоформ.

    Якщо до варіянта ще не додано жодної словоформи, ручна частота варіянта
    зберігається без змін.
    """
    word_id: int | None = None
    with ENGINE.begin() as conn:
        row = _fetchone(conn, 'SELECT "СЛОВО_ID", "ЧАСТОТА" FROM "ВАРІЯНТ" WHERE "ID" = :variant_id', {"variant_id": int(variant_id)})
        if not row:
            return 0
        word_id = int(row["СЛОВО_ID"])
        stats = _fetchone(
            conn,
            'SELECT COUNT(*) AS "CNT", COALESCE(SUM("ЧАСТОТА"), 0) AS "TOTAL" FROM "ВАРІЯНТ-СЛОВОФОРМА" WHERE "ВАРІЯНТ_ID" = :variant_id',
            {"variant_id": int(variant_id)},
        ) or {"CNT": 0, "TOTAL": 0}
        if int(stats.get("CNT") or 0) > 0:
            total = int(stats.get("TOTAL") or 0)
            conn.execute(
                text('UPDATE "ВАРІЯНТ" SET "ЧАСТОТА" = :total WHERE "ID" = :variant_id'),
                {"total": total, "variant_id": int(variant_id)},
            )
        else:
            total = int(row.get("ЧАСТОТА") or 0)
    if word_id:
        recompute_word_frequency(word_id)
    return int(total)


def upsert_variant(variant_id: int | None, word_id: int, variant: str, stressed_form: str, frequency: int, variant_type: str, comment: str) -> int:  # type: ignore[no-redef]
    """Створює або оновлює варіянт і включає його частоту в загальну частоту леми."""
    params = {
        "id": int(variant_id or 0),
        "word_id": int(word_id),
        "variant": str(variant or "").strip(),
        "stressed": str(stressed_form or "").strip(),
        "frequency": int(frequency or 0),
        "variant_type": str(variant_type or "").strip(),
        "comment": str(comment or "").strip(),
    }
    if not params["variant"]:
        return 0
    with ENGINE.begin() as conn:
        if variant_id:
            conn.execute(
                text('UPDATE "ВАРІЯНТ" SET "ВАРІЯНТ"=:variant, "НАГОЛОШЕНА ФОРМА"=:stressed, "ЧАСТОТА"=:frequency, "ТИП"=:variant_type, "КОМЕНТАР"=:comment WHERE "ID"=:id'),
                params,
            )
            out_id = int(variant_id)
        else:
            if IS_POSTGRES:
                out_id = int(_scalar(
                    conn,
                    'INSERT INTO "ВАРІЯНТ"("СЛОВО_ID", "ВАРІЯНТ", "НАГОЛОШЕНА ФОРМА", "ЧАСТОТА", "ТИП", "КОМЕНТАР") VALUES (:word_id, :variant, :stressed, :frequency, :variant_type, :comment) RETURNING "ID"',
                    params,
                ))
            else:
                conn.execute(
                    text('INSERT INTO "ВАРІЯНТ"("СЛОВО_ID", "ВАРІЯНТ", "НАГОЛОШЕНА ФОРМА", "ЧАСТОТА", "ТИП", "КОМЕНТАР") VALUES (:word_id, :variant, :stressed, :frequency, :variant_type, :comment)'),
                    params,
                )
                out_id = _last_insert_id(conn)
    # Якщо для варіянта вже заведено словоформи, ручна частота заміниться їхньою сумою.
    recompute_variant_frequency(out_id)
    recompute_word_frequency(int(word_id))
    return int(out_id)


def list_variants(word_id: int) -> list[dict]:  # type: ignore[no-redef]
    with ENGINE.begin() as conn:
        rows = _fetchall(conn, 'SELECT * FROM "ВАРІЯНТ" WHERE "СЛОВО_ID" = :word_id ORDER BY "ID"', {"word_id": int(word_id)})
    for row in rows:
        row["wordforms"] = list_variant_wordforms(int(row["ID"]))
        row["ЧАСТОТА_СЛОВОФОРМ"] = sum(int(wf.get("ЧАСТОТА") or 0) for wf in row["wordforms"])
        row["КІЛЬКІСТЬ_СЛОВОФОРМ"] = len(row["wordforms"])
    return rows


def delete_variant(variant_id: int) -> None:  # type: ignore[no-redef]
    word_id: int | None = None
    with ENGINE.begin() as conn:
        row = _fetchone(conn, 'SELECT "СЛОВО_ID" FROM "ВАРІЯНТ" WHERE "ID"=:id', {"id": int(variant_id)})
        if row:
            word_id = int(row["СЛОВО_ID"])
        conn.execute(text('DELETE FROM "ВАРІЯНТ" WHERE "ID"=:id'), {"id": int(variant_id)})
    if word_id:
        recompute_word_frequency(word_id)


def list_variant_wordforms(variant_id: int) -> list[dict]:
    with ENGINE.begin() as conn:
        return _fetchall(
            conn,
            'SELECT vwf.*, t."ТЛУМАЧЕННЯ" AS "ЗНАЧЕННЯ", t."ID" AS "ЗНАЧЕННЯ_ID" '
            'FROM "ВАРІЯНТ-СЛОВОФОРМА" vwf '
            'LEFT JOIN "ТЛУМАЧЕННЯ" t ON t."ID" = vwf."ТЛУМАЧЕННЯ_ID" '
            'WHERE vwf."ВАРІЯНТ_ID" = :variant_id '
            'ORDER BY COALESCE(vwf."ГРАМАТИЧНІ ОЗНАКИ", \'\'), vwf."СЛОВОФОРМА", COALESCE(vwf."ТЛУМАЧЕННЯ_ID", 0), vwf."ID"',
            {"variant_id": int(variant_id)},
        )


def upsert_variant_wordform(
    variant_wordform_id: int | None,
    variant_id: int,
    wordform: str,
    frequency: int,
    grammar: str = "",
    stylistics: str = "",
    definition_id: int | None = None,
) -> int:
    """Створює або оновлює словоформу варіянта й перераховує частоту варіянта."""
    wordform = str(wordform or "").strip()
    if not wordform:
        return 0
    params = {
        "id": int(variant_wordform_id or 0),
        "variant_id": int(variant_id),
        "wordform": wordform,
        "frequency": int(frequency or 0),
        "grammar": str(grammar or "").strip(),
        "stylistics": str(stylistics or "").strip(),
        "definition_id": int(definition_id) if definition_id else None,
    }
    with ENGINE.begin() as conn:
        if variant_wordform_id:
            conn.execute(
                text('UPDATE "ВАРІЯНТ-СЛОВОФОРМА" SET "СЛОВОФОРМА"=:wordform, "ЧАСТОТА"=:frequency, "ГРАМАТИЧНІ ОЗНАКИ"=:grammar, "СТИЛІСТИКА"=:stylistics, "ТЛУМАЧЕННЯ_ID"=:definition_id WHERE "ID"=:id'),
                params,
            )
            out_id = int(variant_wordform_id)
        else:
            # Та сама графічна форма може дублюватися, якщо відрізняється граматика, стилістика або значення.
            row = _fetchone(
                conn,
                'SELECT "ID" FROM "ВАРІЯНТ-СЛОВОФОРМА" WHERE "ВАРІЯНТ_ID"=:variant_id AND "СЛОВОФОРМА"=:wordform '
                'AND COALESCE("ГРАМАТИЧНІ ОЗНАКИ", \'\') = COALESCE(:grammar, \'\') '
                'AND COALESCE("СТИЛІСТИКА", \'\') = COALESCE(:stylistics, \'\') '
                'AND COALESCE("ТЛУМАЧЕННЯ_ID", -1) = COALESCE(:definition_id, -1) ORDER BY "ID" LIMIT 1',
                params,
            )
            if row:
                out_id = int(row["ID"])
                params["id"] = out_id
                conn.execute(
                    text('UPDATE "ВАРІЯНТ-СЛОВОФОРМА" SET "ЧАСТОТА"=:frequency WHERE "ID"=:id'),
                    params,
                )
            elif IS_POSTGRES:
                out_id = int(_scalar(
                    conn,
                    'INSERT INTO "ВАРІЯНТ-СЛОВОФОРМА"("ВАРІЯНТ_ID", "СЛОВОФОРМА", "ЧАСТОТА", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА", "ТЛУМАЧЕННЯ_ID") VALUES (:variant_id, :wordform, :frequency, :grammar, :stylistics, :definition_id) RETURNING "ID"',
                    params,
                ))
            else:
                conn.execute(
                    text('INSERT INTO "ВАРІЯНТ-СЛОВОФОРМА"("ВАРІЯНТ_ID", "СЛОВОФОРМА", "ЧАСТОТА", "ГРАМАТИЧНІ ОЗНАКИ", "СТИЛІСТИКА", "ТЛУМАЧЕННЯ_ID") VALUES (:variant_id, :wordform, :frequency, :grammar, :stylistics, :definition_id)'),
                    params,
                )
                out_id = _last_insert_id(conn)
    recompute_variant_frequency(int(variant_id))
    return int(out_id)


def delete_variant_wordform(variant_wordform_id: int) -> None:
    variant_id: int | None = None
    with ENGINE.begin() as conn:
        row = _fetchone(conn, 'SELECT "ВАРІЯНТ_ID" FROM "ВАРІЯНТ-СЛОВОФОРМА" WHERE "ID"=:id', {"id": int(variant_wordform_id)})
        if row:
            variant_id = int(row["ВАРІЯНТ_ID"])
        conn.execute(text('DELETE FROM "ВАРІЯНТ-СЛОВОФОРМА" WHERE "ID"=:id'), {"id": int(variant_wordform_id)})
    if variant_id:
        recompute_variant_frequency(variant_id)


_PREV_GET_DICTIONARY_ARTICLE_FULL_VARIANT_WF = get_dictionary_article_full


def get_dictionary_article_full(word_id: int) -> dict[str, Any]:  # type: ignore[no-redef]
    # Спершу синхронізуємо загальну частоту, щоб у перегляді вона вже містила варіянти.
    recompute_word_frequency(int(word_id))
    article = _PREV_GET_DICTIONARY_ARTICLE_FULL_VARIANT_WF(word_id)
    variants = list_variants(word_id)
    article["variants"] = variants
    # Після оновлення варіянтів ще раз дістаємо слово з актуальною загальною частотою.
    article["word"] = get_word(word_id)
    return article
