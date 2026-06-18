# -*- coding: utf-8 -*-
"""Оптимізований Streamlit-інтерфейс електронного конкордансу творів-першодруків Степана Бандери."""

from __future__ import annotations

import base64
import html
import io
import json
import random
import re
import smtplib
import sqlite3
import zipfile
from pathlib import Path
from email.message import EmailMessage
from typing import Iterable

import pandas as pd
import streamlit as st

from variant_rules import normalize_wordform, variant_key, variant_group_codes

import dictionary_db as lexdb

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = PROJECT_DIR / "outputs"
DB_PATH = OUTPUTS_DIR / "concordance_index.sqlite"
ASSETS_DIR = PROJECT_DIR / "assets"
PORTRAIT_PATH = ASSETS_DIR / "stepan_bandera_portrait.jpg"
PORTRAIT_CANDIDATES = [
    PORTRAIT_PATH,
    ASSETS_DIR / "stepan_bandera_portrait.jpeg",
    ASSETS_DIR / "stepan_bandera_portrait.png",
    ASSETS_DIR / "bandera.jpg",
    ASSETS_DIR / "bandera.jpeg",
    ASSETS_DIR / "bandera.png",
]
EMPTY_CONTEXT = "------"
EXCLUDED_VARIANT_GROUPS_PATH = OUTPUTS_DIR / "excluded_variant_groups.json"

DEFAULT_ADMIN_NAME = "Владислав Кривенок"
DEFAULT_ADMIN_EMAIL = "kryvenokvladyslav@gmail.com"


def app_secret_value(name: str, default: str = "") -> str:
    """Безпечно читає значення зі Streamlit Secrets або змінних середовища."""
    try:
        if name in st.secrets:
            return str(st.secrets[name]).strip()
        if "email" in st.secrets and isinstance(st.secrets["email"], dict):
            section = st.secrets["email"]
            for key in (name, name.lower(), name.upper()):
                if key in section:
                    return str(section[key]).strip()
    except Exception:
        pass
    import os
    return str(os.environ.get(name, default) or default).strip()


def notify_admin_about_access_request(name: str, email: str) -> bool:
    """Надсилає адміністраторові email про новий запит, якщо налаштовано SMTP-secrets."""
    smtp_host = app_secret_value("SMTP_HOST")
    smtp_user = app_secret_value("SMTP_USERNAME")
    smtp_password = app_secret_value("SMTP_PASSWORD")
    smtp_from = app_secret_value("SMTP_FROM", smtp_user or DEFAULT_ADMIN_EMAIL)
    admin_email = app_secret_value("ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL)
    if not smtp_host or not smtp_user or not smtp_password or not admin_email:
        return False
    try:
        smtp_port = int(app_secret_value("SMTP_PORT", "587") or "587")
        msg = EmailMessage()
        msg["Subject"] = "Бандерографія: новий запит на доступ до словника"
        msg["From"] = smtp_from
        msg["To"] = admin_email
        msg.set_content(
            "Новий користувач просить доступ до редагування словника.\n\n"
            f"Ім’я: {name}\n"
            f"Пошта: {email}\n\n"
            "Щоб надати або відхилити доступ, відкрийте вкладку «Словник» → «Редагувати» "
            "і скористайтеся адмін-панеллю керування доступом."
        )
        with smtplib.SMTP(smtp_host, smtp_port, timeout=12) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True
    except Exception:
        return False


CONCORDANCE_COLUMNS_UA = {
    "wordform": "Реєстрова словоформа",
    "frequency": "Частота словоформи",
    "variant_key": "Варіянтний ключ",
    "keyword": "Словоформа в контексті",
    "left_context": "Лівий контекст",
    "right_context": "Правий контекст",
    "code": "Код статті",
    "article_title": "Назва статті",
    "article_id": "ID статті",
    "token_index": "Позиція словоформи",
}

CONCORDANCE_EXPORT_ORDER = [
    "wordform",
    "keyword",
    "left_context",
    "right_context",
    "code",
    "article_title",
    "article_id",
    "token_index",
]


def concordance_export_df(df: pd.DataFrame) -> pd.DataFrame:
    """Готує конкорданс до завантаження: українські назви колонок і сталий порядок."""
    if df.empty:
        return df.copy()
    ordered_existing = [col for col in CONCORDANCE_EXPORT_ORDER if col in df.columns]
    remaining = [col for col in df.columns if col not in ordered_existing]
    out = df[ordered_existing + remaining].copy()
    return out.rename(columns=CONCORDANCE_COLUMNS_UA)


st.set_page_config(
    page_title="БАНДЕРОГРАФІЯ",
    page_icon="📚",
    layout="wide",
)


def db_stamp() -> int:
    """Версія бази для автоматичного скидання кешу після перебудови індексу."""
    return DB_PATH.stat().st_mtime_ns if DB_PATH.exists() else 0


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # Трохи пришвидшує багато читань із SQLite у локальному застосунку.
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -80000")
    return conn


@st.cache_data(show_spinner=False)
def load_wordforms(_stamp: int) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame(columns=["wordform", "frequency", "variant_key"])
    conn = db_connect()
    df = pd.read_sql_query(
        "SELECT wordform, frequency, variant_key FROM wordforms ORDER BY frequency DESC, wordform ASC",
        conn,
    )
    conn.close()
    if not df.empty:
        # Важливо: ключ варіянтности обчислюємо актуальними правилами з variant_rules.py,
        # а не довіряємо старому ключу, який міг лишитися в SQLite після попередньої перебудови.
        df["variant_key"] = df["wordform"].astype(str).apply(variant_key)
    return df


@st.cache_data(show_spinner=False)
def load_articles(_stamp: int) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame(columns=["article_id", "code", "title", "tokens"])
    conn = db_connect()
    df = pd.read_sql_query(
        "SELECT article_id, code, title, tokens FROM articles ORDER BY article_id ASC",
        conn,
    )
    conn.close()
    return df


def variant_words_from_variants_text(variants_text: str) -> list[str]:
    """Витягує словоформи з рядка типу 'СЛОВО 3 / СЛОВО 1'."""
    words: list[str] = []
    for part in str(variants_text or "").split(" / "):
        word = re.sub(r"\s+\d+\s*$", "", part.strip())
        if word:
            words.append(normalize_wordform(word))
    return words


def variant_group_signature(words: Iterable[str]) -> str:
    return "|".join(sorted({normalize_wordform(w) for w in words if normalize_wordform(w)}))


def load_excluded_variant_groups() -> list[dict]:
    if not EXCLUDED_VARIANT_GROUPS_PATH.exists():
        return []
    try:
        data = json.loads(EXCLUDED_VARIANT_GROUPS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def excluded_variant_signatures() -> set[str]:
    return {str(item.get("signature", "")) for item in load_excluded_variant_groups() if item.get("signature")}


def excluded_variant_keys() -> set[str]:
    return {str(item.get("variant_key", "")) for item in load_excluded_variant_groups() if item.get("variant_key")}


def save_excluded_variant_groups(items: list[dict]) -> None:
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    EXCLUDED_VARIANT_GROUPS_PATH.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_excluded_variant_group(row: pd.Series) -> None:
    words = variant_words_from_variants_text(row.get("variants", ""))
    signature = variant_group_signature(words)
    if not signature:
        return
    items = load_excluded_variant_groups()
    if any(item.get("signature") == signature for item in items):
        return
    items.append({
        "signature": signature,
        "variant_key": str(row.get("variant_key", "")),
        "variant_codes": str(row.get("variant_codes", "")),
        "variants": str(row.get("variants", "")),
    })
    save_excluded_variant_groups(items)


def restore_excluded_variant_group(signature: str) -> None:
    items = [item for item in load_excluded_variant_groups() if item.get("signature") != signature]
    save_excluded_variant_groups(items)


@st.cache_data(show_spinner=False)
def load_variant_groups(_stamp: int) -> pd.DataFrame:
    """Будує звіт варіянтних груп актуальними правилами й ураховує вилучення."""
    wf = load_wordforms(_stamp)
    if wf.empty:
        return pd.DataFrame(columns=["variant_key", "variant_codes", "variants", "total_frequency", "signature"])

    excluded = excluded_variant_signatures()
    grouped = []
    for key, group in wf.groupby("variant_key"):
        if len(group) < 2:
            continue
        group = group.sort_values(["frequency", "wordform"], ascending=[False, True])
        words = group["wordform"].astype(str).tolist()
        signature = variant_group_signature(words)
        if signature in excluded:
            continue
        variants = " / ".join(f"{r.wordform} {int(r.frequency)}" for r in group.itertuples())
        total = int(group["frequency"].sum())
        codes = variant_group_codes(words) or "інше"
        grouped.append({
            "variant_key": key,
            "variant_codes": codes,
            "variants": variants,
            "total_frequency": total,
            "signature": signature,
        })
    if not grouped:
        return pd.DataFrame(columns=["variant_key", "variant_codes", "variants", "total_frequency", "signature"])
    return pd.DataFrame(grouped).sort_values("total_frequency", ascending=False)


@st.cache_data(show_spinner=False)
def get_article_text(_stamp: int, article_id: int) -> dict | None:
    if not DB_PATH.exists():
        return None
    conn = db_connect()
    row = conn.execute(
        "SELECT article_id, code, title, text, tokens FROM articles WHERE article_id = ?",
        (int(article_id),),
    ).fetchone()
    conn.close()
    return dict(row) if row is not None else None


@st.cache_data(show_spinner=False)
def load_all_article_texts(_stamp: int) -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame(columns=["article_id", "code", "title", "text", "tokens"])
    conn = db_connect()
    df = pd.read_sql_query(
        "SELECT article_id, code, title, text, tokens FROM articles ORDER BY article_id ASC",
        conn,
    )
    conn.close()
    return df


def clean_context_fragment(text: str) -> str:
    text = str(text or "")
    text = text.replace("\u00ad", "").replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_queries(text: str) -> list[str]:
    parts = re.split(r"[,;\n]+", text or "")
    result: list[str] = []
    seen: set[str] = set()
    for p in parts:
        q = normalize_wordform(p)
        if q and q not in seen:
            seen.add(q)
            result.append(q)
    return result


def get_wordforms_for_queries(queries: list[str], use_variants: bool, wordforms_df: pd.DataFrame) -> list[str]:
    if wordforms_df.empty:
        return []
    existing = set(wordforms_df["wordform"].astype(str))
    if not use_variants:
        return [q for q in queries if q in existing]

    excluded_keys = excluded_variant_keys()
    result: list[str] = []
    seen: set[str] = set()
    for q in queries:
        key = variant_key(q)
        if key in excluded_keys:
            # Якщо групу вилучено вручну, пошук за цією словоформою більше не
            # розширюємо до всієї варіянтної групи.
            candidates = [q] if q in existing else []
        else:
            candidates = wordforms_df[wordforms_df["variant_key"].eq(key)].sort_values(
                ["frequency", "wordform"], ascending=[False, True]
            )["wordform"].astype(str).tolist()
        for wf in candidates:
            if wf not in seen:
                result.append(wf)
                seen.add(wf)
    return result


def format_register_header(found_wordforms: list[str], wordforms_df: pd.DataFrame, use_variants: bool) -> str:
    if not found_wordforms:
        return ""
    freq_map = dict(zip(wordforms_df["wordform"], wordforms_df["frequency"]))
    parts = [f"{wf} {int(freq_map.get(wf, 0))}" for wf in found_wordforms]
    total = sum(int(freq_map.get(wf, 0)) for wf in found_wordforms)
    if use_variants and len(found_wordforms) > 1:
        return " / ".join(parts) + f" | {total}"
    if len(found_wordforms) > 1:
        return " / ".join(parts) + f" | {total}"
    return parts[0]


def sql_placeholders(items: Iterable[object]) -> str:
    return ",".join("?" for _ in items)


@st.cache_data(show_spinner=False)
def fetch_available_codes(_stamp: int, wordforms: tuple[str, ...]) -> pd.DataFrame:
    """Повертає тільки ті статті, у яких трапляються знайдені словоформи.

    Це набагато швидше, ніж спочатку витягувати всі контексти, а тоді шукати
    у них коди статей.
    """
    if not DB_PATH.exists() or not wordforms:
        return pd.DataFrame(columns=["code", "title", "hits"])

    conn = db_connect()
    placeholders = sql_placeholders(wordforms)
    df = pd.read_sql_query(
        f"""
        SELECT a.code AS code, a.title AS title, COUNT(*) AS hits
        FROM tokens t
        JOIN articles a ON a.article_id = t.article_id
        WHERE t.norm_wordform IN ({placeholders})
        GROUP BY a.article_id, a.code, a.title
        ORDER BY a.article_id ASC
        """,
        conn,
        params=tuple(wordforms),
    )
    conn.close()
    return df


def get_context_sentence(article_text: str, row: sqlite3.Row) -> tuple[str, str]:
    left = clean_context_fragment(article_text[row["sentence_start"]:row["char_start"]])
    right = clean_context_fragment(article_text[row["char_end"]:row["sentence_end"]])
    return left or EMPTY_CONTEXT, right or EMPTY_CONTEXT


def get_context_depth(conn: sqlite3.Connection, article_text: str, row: sqlite3.Row, depth: int) -> tuple[str, str]:
    article_id = row["article_id"]
    token_index = row["token_index"]
    left_boundary = conn.execute(
        """
        SELECT char_start FROM tokens
        WHERE article_id = ? AND token_index >= ? AND token_index < ?
        ORDER BY token_index ASC LIMIT 1
        """,
        (article_id, max(0, token_index - depth), token_index),
    ).fetchone()
    right_boundary = conn.execute(
        """
        SELECT char_end FROM tokens
        WHERE article_id = ? AND token_index > ? AND token_index <= ?
        ORDER BY token_index DESC LIMIT 1
        """,
        (article_id, token_index, token_index + depth),
    ).fetchone()
    left = clean_context_fragment(article_text[left_boundary["char_start"]:row["char_start"]]) if left_boundary else ""
    right = clean_context_fragment(article_text[row["char_end"]:right_boundary["char_end"]]) if right_boundary else ""
    return left or EMPTY_CONTEXT, right or EMPTY_CONTEXT


@st.cache_data(show_spinner=False)
def fetch_contexts(
    _stamp: int,
    wordforms: tuple[str, ...],
    mode: str,
    depth: int,
    selected_codes: tuple[str, ...] = (),
    limit: int = 200,
) -> pd.DataFrame:
    """Витягує з бази тільки потрібну кількість контекстів.

    Попередня версія спершу діставала всі контексти, а вже потім обрізала їх
    для показу. Для дуже частотних слів це могло навантажувати сайт.
    """
    if not DB_PATH.exists() or not wordforms:
        return pd.DataFrame()

    conn = db_connect()
    wf_placeholders = sql_placeholders(wordforms)
    params: list[object] = list(wordforms)
    code_clause = ""
    if selected_codes:
        code_placeholders = sql_placeholders(selected_codes)
        code_clause = f" AND a.code IN ({code_placeholders})"
        params.extend(selected_codes)

    limit_clause = ""
    if limit and limit > 0:
        limit_clause = " LIMIT ?"
        params.append(int(limit))

    rows = conn.execute(
        f"""
        SELECT t.*, a.code, a.title AS article_title, a.text AS article_text
        FROM tokens t
        JOIN articles a ON a.article_id = t.article_id
        WHERE t.norm_wordform IN ({wf_placeholders})
        {code_clause}
        ORDER BY t.norm_wordform ASC, a.article_id ASC, t.token_index ASC
        {limit_clause}
        """,
        tuple(params),
    ).fetchall()

    out = []
    for row in rows:
        if mode == "Реченнєвий контекст":
            left, right = get_context_sentence(row["article_text"], row)
        else:
            left, right = get_context_depth(conn, row["article_text"], row, int(depth))
        out.append({
            "wordform": row["norm_wordform"],
            "keyword": row["wordform"],
            "left_context": left,
            "right_context": right,
            "code": row["code"],
            "article_title": row["article_title"],
            "article_id": row["article_id"],
            "token_index": row["token_index"],
        })
    conn.close()
    return pd.DataFrame(out)


@st.cache_data(show_spinner=False)
def count_contexts(_stamp: int, wordforms: tuple[str, ...], selected_codes: tuple[str, ...] = ()) -> int:
    if not DB_PATH.exists() or not wordforms:
        return 0
    conn = db_connect()
    wf_placeholders = sql_placeholders(wordforms)
    params: list[object] = list(wordforms)
    code_clause = ""
    if selected_codes:
        code_placeholders = sql_placeholders(selected_codes)
        code_clause = f" AND a.code IN ({code_placeholders})"
        params.extend(selected_codes)
    n = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM tokens t
        JOIN articles a ON a.article_id = t.article_id
        WHERE t.norm_wordform IN ({wf_placeholders})
        {code_clause}
        """,
        tuple(params),
    ).fetchone()[0]
    conn.close()
    return int(n)


def render_kwic_html(result_df: pd.DataFrame) -> str:
    """Рендерить усі KWIC-рядки одним HTML-блоком, а не сотнями st.markdown.

    Це одна з головних оптимізацій інтерфейсу.
    """
    rows_html: list[str] = []
    for row in result_df.itertuples(index=False):
        rows_html.append(
            "<div class='context-meta'>"
            f"<span class='article-code'>{html.escape(str(row.code))}</span> "
            f"<span class='article-title'>{html.escape(str(row.article_title))}</span>"
            "</div>"
            "<div class='kwic-row'>"
            f"<div class='kwic-left'>{html.escape(str(row.left_context))}</div>"
            f"<div class='kwic-keyword'>{html.escape(str(row.keyword))}</div>"
            f"<div class='kwic-right'>{html.escape(str(row.right_context))}</div>"
            "</div>"
        )
    return "<div class='kwic-wrapper'>" + "".join(rows_html) + "</div>"


def make_articles_zip(articles_df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for row in articles_df.itertuples(index=False):
            filename = f"{row.code} {row.title}.txt"
            filename = re.sub(r"[\\/:*?\"<>|]+", "_", filename)
            content = f"{row.code}\n{row.title}\n\n{row.text}"
            zf.writestr(filename, content.encode("utf-8"))
    return buffer.getvalue()



def dataframe_to_xlsx_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    """Повертає Excel-файл у байтах для завантаження на сайті."""
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            safe_name = re.sub(r"[\\/*?:\[\]]+", " ", str(sheet_name)).strip()[:31] or "Аркуш"
            df.to_excel(writer, sheet_name=safe_name, index=False)
    return buffer.getvalue()


@st.cache_data(show_spinner=False)
def fetch_article_frequency(_stamp: int, article_id: int) -> pd.DataFrame:
    """Частотний список словоформ для однієї статті."""
    if not DB_PATH.exists():
        return pd.DataFrame(columns=["wordform", "frequency"])
    conn = db_connect()
    df = pd.read_sql_query(
        """
        SELECT norm_wordform AS wordform, COUNT(*) AS frequency
        FROM tokens
        WHERE article_id = ?
        GROUP BY norm_wordform
        ORDER BY frequency DESC, wordform ASC
        """,
        conn,
        params=(int(article_id),),
    )
    conn.close()
    return df


@st.cache_data(show_spinner=False)
def fetch_article_counts_for_wordforms(_stamp: int, wordforms: tuple[str, ...]) -> pd.DataFrame:
    """Статистика розподілу знайдених словоформ за статтями."""
    if not DB_PATH.exists() or not wordforms:
        return pd.DataFrame(columns=["code", "title", "frequency"])
    conn = db_connect()
    placeholders = sql_placeholders(wordforms)
    df = pd.read_sql_query(
        f"""
        SELECT a.code AS code, a.title AS title, COUNT(*) AS frequency
        FROM tokens t
        JOIN articles a ON a.article_id = t.article_id
        WHERE t.norm_wordform IN ({placeholders})
        GROUP BY a.article_id, a.code, a.title
        ORDER BY frequency DESC, a.article_id ASC
        """,
        conn,
        params=tuple(wordforms),
    )
    conn.close()
    return df


def extract_variant_type_options(df: pd.DataFrame) -> list[str]:
    """Витягує всі типи варіянтности зі звіту варіянтних слів."""
    if df.empty or "variant_codes" not in df.columns:
        return []
    values: set[str] = set()
    for raw in df["variant_codes"].dropna().astype(str):
        for part in re.split(r"[;,]", raw):
            part = part.strip()
            if part and part.lower() != "інше":
                values.add(part)
    return sorted(values)


def render_register_card(header: str, total_hits: int, article_count: int, mode: str, depth: int) -> str:
    context_label = mode if mode == "Реченнєвий контекст" else f"Контекст фіксованої глибини: {depth}"
    return (
        "<div class='register-card'>"
        f"<div class='headword'>{html.escape(header)}</div>"
        "<div class='register-metrics'>"
        f"<span>Статей: <strong>{int(article_count)}</strong></span>"
        f"<span>{html.escape(context_label)}</span>"
        "</div>"
        "</div>"
    )

def resolve_portrait_path() -> Path | None:
    """Знаходить портрет у папці assets навіть тоді, коли файл має інше розширення."""
    for candidate in PORTRAIT_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def image_data_uri(path: Path | None) -> str:
    """Повертає локальне зображення як data URI для HTML-блоку шапки."""
    if path is None or not path.exists():
        return ""
    suffix = path.suffix.lower().lstrip(".") or "jpeg"
    mime = "jpeg" if suffix in {"jpg", "jpeg"} else suffix
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{encoded}"



PORTRAIT_DATA_URI = image_data_uri(resolve_portrait_path())


# =========================
# Вкладка «Словник»: допоміжні функції
# =========================

ADD_SENTINEL_PREFIX = "➕ Додати нове значення до списку"
VOWELS_UA = set("аеєиіїоуюяАЕЄИІЇОУЮЯ")
_DICT_OPTION_DIALOG_OPENED_THIS_RUN = False


def _split_saved_options(raw: str) -> list[str]:
    return [p.strip() for p in str(raw or "").split(";") if p.strip()]


def safe_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def keyed_container(key: str):
    try:
        return st.container(key=key)
    except TypeError:
        return st.container()


# Легка оптимізація вкладки «Словник» без зміни її функціоналу чи дизайну.
# Streamlit щоразу перезапускає скрипт після дії користувача, тому повторні
# читання незмінних довідників і службових списків кешуємо в межах поточної сесії.
def dictionary_cache_version() -> int:
    return int(st.session_state.get("_dictionary_cache_version", 0) or 0)


def invalidate_dictionary_caches() -> None:
    st.session_state["_dictionary_cache_version"] = dictionary_cache_version() + 1


def fast_rerun() -> None:
    """Перезапуск сторінки після зміни даних із точковим скиданням кешу словника."""
    invalidate_dictionary_caches()
    st.rerun()


def ensure_dictionary_db_ready() -> None:
    """Ініціалізує словникову БД один раз за сесію замість кожного кліку."""
    if st.session_state.get("_dictionary_db_ready"):
        return
    lexdb.init_lex_db()
    lexdb.ensure_default_admin_user()
    st.session_state["_dictionary_db_ready"] = True


@st.cache_data(ttl=600, show_spinner=False)
def _cached_lexdb_get_options(field_name: str, cache_version: int) -> list[str]:
    return lexdb.get_options(field_name)


def cached_lexdb_get_options(field_name: str) -> list[str]:
    return _cached_lexdb_get_options(field_name, dictionary_cache_version())


@st.cache_data(ttl=600, show_spinner=False)
def _cached_lexdb_get_grammar_options(pos: str, cache_version: int) -> list[str]:
    return lexdb.get_grammar_options(pos)


def cached_lexdb_get_grammar_options(pos: str) -> list[str]:
    return _cached_lexdb_get_grammar_options(pos, dictionary_cache_version())


@st.cache_data(ttl=600, show_spinner=False)
def _cached_lexdb_list_references(table: str, limit: int, cache_version: int) -> list[dict]:
    return lexdb.list_references(table, limit=limit)


def cached_lexdb_list_references(table: str, limit: int = 10000) -> list[dict]:
    return _cached_lexdb_list_references(table, int(limit), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_search_words(query: str, limit: int, cache_version: int) -> list[dict]:
    return lexdb.search_words(query, limit=limit)


def cached_lexdb_search_words(query: str = "", limit: int = 200) -> list[dict]:
    return _cached_lexdb_search_words(str(query or ""), int(limit), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_get_dictionary_article_full(word_id: int, cache_version: int) -> dict:
    return lexdb.get_dictionary_article_full(int(word_id))


def cached_lexdb_get_dictionary_article_full(word_id: int) -> dict:
    return _cached_lexdb_get_dictionary_article_full(int(word_id), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_list_users(cache_version: int) -> list[dict]:
    return lexdb.list_users()


def cached_lexdb_list_users() -> list[dict]:
    return _cached_lexdb_list_users(dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_list_definitions(word_id: int, cache_version: int) -> list[dict]:
    return lexdb.list_definitions(int(word_id))


def cached_lexdb_list_definitions(word_id: int) -> list[dict]:
    return _cached_lexdb_list_definitions(int(word_id), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_list_collocations(word_id: int, cache_version: int) -> list[dict]:
    return lexdb.list_collocations(int(word_id))


def cached_lexdb_list_collocations(word_id: int) -> list[dict]:
    return _cached_lexdb_list_collocations(int(word_id), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_list_wordforms_for_word(word_id: int, cache_version: int) -> list[dict]:
    return lexdb.list_wordforms_for_word(int(word_id))


def cached_lexdb_list_wordforms_for_word(word_id: int) -> list[dict]:
    return _cached_lexdb_list_wordforms_for_word(int(word_id), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_list_variants(word_id: int, cache_version: int) -> list[dict]:
    return lexdb.list_variants(int(word_id))


def cached_lexdb_list_variants(word_id: int) -> list[dict]:
    return _cached_lexdb_list_variants(int(word_id), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_list_variant_wordforms(variant_id: int, cache_version: int) -> list[dict]:
    return lexdb.list_variant_wordforms(int(variant_id))


def cached_lexdb_list_variant_wordforms(variant_id: int) -> list[dict]:
    return _cached_lexdb_list_variant_wordforms(int(variant_id), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_list_crossrefs(word_id: int, cache_version: int) -> list[dict]:
    return lexdb.list_crossrefs(int(word_id))


def cached_lexdb_list_crossrefs(word_id: int) -> list[dict]:
    return _cached_lexdb_list_crossrefs(int(word_id), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_list_semantic_relations(word_id: int, cache_version: int) -> list[dict]:
    return lexdb.list_semantic_relations(int(word_id))


def cached_lexdb_list_semantic_relations(word_id: int) -> list[dict]:
    return _cached_lexdb_list_semantic_relations(int(word_id), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_list_subdefinitions(definition_id: int, cache_version: int) -> list[dict]:
    return lexdb.list_subdefinitions(int(definition_id))


def cached_lexdb_list_subdefinitions(definition_id: int) -> list[dict]:
    return _cached_lexdb_list_subdefinitions(int(definition_id), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_get_wordform(word_id: int, wordform: str, cache_version: int) -> dict | None:
    return lexdb.get_wordform(int(word_id), str(wordform or ""))


def cached_lexdb_get_wordform(word_id: int, wordform: str) -> dict | None:
    return _cached_lexdb_get_wordform(int(word_id), str(wordform or ""), dictionary_cache_version())


@st.cache_data(ttl=120, show_spinner=False)
def _cached_lexdb_get_quote_attachments(quote_id: int, cache_version: int) -> list[dict]:
    return lexdb.get_quote_attachments(int(quote_id))


def cached_lexdb_get_quote_attachments(quote_id: int) -> list[dict]:
    return _cached_lexdb_get_quote_attachments(int(quote_id), dictionary_cache_version())


def _open_add_option_dialog(field_name: str, label: str, key_prefix: str) -> None:
    # Streamlit дозволяє відкрити лише один st.dialog за один прохід скрипта.
    # Якщо sentinel «Додати нове значення...» випадково лишився вибраним у кількох
    # multiselect/selectbox, відкриваємо тільки перше вікно, щоб сторінка не падала.
    global _DICT_OPTION_DIALOG_OPENED_THIS_RUN
    if _DICT_OPTION_DIALOG_OPENED_THIS_RUN:
        return
    _DICT_OPTION_DIALOG_OPENED_THIS_RUN = True

    title = f"Додати нове значення до списку «{label}»"

    def fallback_form() -> None:
        with st.expander(title, expanded=True):
            new_value = st.text_input("Нове значення", key=f"{key_prefix}_{field_name}_dialog_value_fallback")
            if st.button("Зберегти значення", key=f"{key_prefix}_{field_name}_dialog_save_fallback"):
                if not new_value.strip():
                    st.error("Нове значення не може бути порожнім.")
                else:
                    lexdb.add_option(field_name, new_value)
                    st.success("Значення додано до випадного списку.")
                    fast_rerun()

    if hasattr(st, "dialog"):
        @st.dialog(title)
        def add_dialog() -> None:
            st.caption("Після збереження значення з’явиться у відповідному випадному списку.")
            new_value = st.text_input("Нове значення", key=f"{key_prefix}_{field_name}_dialog_value")
            if st.button("Зберегти значення", type="primary", key=f"{key_prefix}_{field_name}_dialog_save"):
                if not new_value.strip():
                    st.error("Нове значення не може бути порожнім.")
                else:
                    lexdb.add_option(field_name, new_value)
                    st.success("Значення додано до випадного списку.")
                    fast_rerun()
        add_dialog()
    else:
        fallback_form()


def dictionary_option_select(label: str, field_name: str, default_value: str = "", key_prefix: str = "") -> str:
    options = cached_lexdb_get_options(field_name)
    shown_base = list(options)
    if default_value and default_value not in shown_base:
        shown_base.append(default_value)

    empty_option = "— не вибрано —"
    sentinel = f"{ADD_SENTINEL_PREFIX} «{label}»"
    shown_options = [empty_option] + shown_base + [sentinel]
    index = shown_options.index(default_value) if default_value in shown_options else 0
    value = st.selectbox(label, options=shown_options, index=index, key=f"{key_prefix}_{field_name}_select")

    if value == sentinel:
        _open_add_option_dialog(field_name, label, key_prefix)
        return default_value if default_value in shown_base else ""
    if value == empty_option:
        return ""
    return value


def dictionary_option_multiselect(label: str, field_name: str, default_raw: str = "", key_prefix: str = "", options_override: list[str] | None = None) -> str:
    options = list(options_override if options_override is not None else cached_lexdb_get_options(field_name))
    defaults_raw = _split_saved_options(default_raw)
    shown_base = list(options)
    for value in defaults_raw:
        if value and value not in shown_base:
            shown_base.append(value)
    defaults = [v for v in defaults_raw if v in shown_base]
    sentinel = f"{ADD_SENTINEL_PREFIX} «{label}»"
    shown_options = shown_base + [sentinel]
    selected = st.multiselect(label, options=shown_options, default=defaults, key=f"{key_prefix}_{field_name}_multi")
    if sentinel in selected:
        _open_add_option_dialog(field_name, label, key_prefix)
    selected_clean = [v for v in selected if v != sentinel]
    return lexdb.normalize_join(selected_clean)


def grammar_multiselect_for_pos(label: str, pos: str, default_raw: str = "", key_prefix: str = "") -> str:
    grammar_field = lexdb.grammar_field_name(pos)
    options = cached_lexdb_get_grammar_options(pos)
    return dictionary_option_multiselect(label, grammar_field, default_raw, key_prefix=key_prefix, options_override=options)


def render_dictionary_auth() -> dict | None:
    ensure_dictionary_db_ready()

    if "dictionary_user" in st.session_state:
        user = st.session_state["dictionary_user"]
        top1, top2 = st.columns([4, 1])
        with top1:
            st.success(f"Ви ввійшли як {user.get('NAME')} ({user.get('EMAIL')}).")
        with top2:
            if st.button("Вийти", key="dict_logout"):
                st.session_state.pop("dictionary_user", None)
                fast_rerun()
        if not int(user.get("CAN_EDIT", 0)):
            st.warning("Ваш обліковий запис створено, але право редагування словника ще не надано адміністратором.")
            return None
        return user


    login_tab, register_tab = st.tabs(["Вхід", "Запит на доступ"])
    with login_tab:
        with st.form("dictionary_login_form"):
            email = st.text_input("Пошта", key="dict_login_email")
            password = st.text_input("Пароль", type="password", key="dict_login_password")
            submitted = st.form_submit_button("Увійти", type="primary")
        if submitted:
            user = lexdb.verify_user(email, password)
            if user is None:
                st.error("Неправильна пошта або пароль.")
            else:
                st.session_state["dictionary_user"] = user
                fast_rerun()
    with register_tab:
        st.caption("Після реєстрації адміністратор має надати право редагування.")
        with st.form("dictionary_register_form"):
            name = st.text_input("Ім’я", key="dict_reg_name")
            email = st.text_input("Пошта", key="dict_reg_email")
            password = st.text_input("Пароль", type="password", key="dict_reg_password")
            submitted = st.form_submit_button("Надіслати запит")
        if submitted:
            try:
                lexdb.create_user(name, email, password, role="viewer", can_edit=False)
                notified = notify_admin_about_access_request(name, email)
                if notified:
                    st.success("Запит створено. Адміністраторові надіслано сповіщення на пошту.")
                else:
                    st.success("Запит створено. Дочекайтеся дозволу адміністратора.")
                    st.caption("Щоб сайт надсилав email-сповіщення автоматично, потрібно додати SMTP-параметри в Streamlit Secrets.")
            except Exception as exc:
                st.error(f"Не вдалося створити обліковий запис: {exc}")
    return None


def render_dictionary_admin_panel(current_user: dict) -> None:
    if current_user.get("ROLE") != "admin":
        return
    with st.expander("Керування доступом до редагування словника", expanded=False):
        users = cached_lexdb_list_users()
        if not users:
            st.info("Користувачів ще немає.")
            return
        users_df = pd.DataFrame(users).rename(columns={
            "ID": "ID", "NAME": "Ім’я", "EMAIL": "Пошта", "ROLE": "Роль", "CAN_EDIT": "Може редагувати", "CREATED_AT": "Створено"
        })
        st.dataframe(users_df, width="stretch", hide_index=True)
        user_ids = [u["ID"] for u in users]
        labels = {u["ID"]: f"{u['NAME']} — {u['EMAIL']}" for u in users}
        selected_id = st.selectbox("Користувач", options=user_ids, format_func=lambda x: labels.get(x, str(x)), key="admin_user_select")
        selected_user = next(u for u in users if u["ID"] == selected_id)
        role = st.selectbox("Роль", options=["viewer", "editor", "admin"], index=["viewer", "editor", "admin"].index(selected_user["ROLE"]), key="admin_role_select")
        can_edit = st.checkbox("Дозволити редагування словника", value=bool(selected_user["CAN_EDIT"]), key="admin_can_edit")
        action_col1, action_col2, action_col3 = st.columns([1, 1, 1])
        with action_col1:
            if st.button("Зберегти права", key="admin_save_permissions"):
                lexdb.set_user_permission(int(selected_id), role, can_edit)
                if st.session_state.get("dictionary_user", {}).get("ID") == selected_id:
                    st.session_state["dictionary_user"]["ROLE"] = role
                    st.session_state["dictionary_user"]["CAN_EDIT"] = int(can_edit)
                st.success("Права оновлено.")
                fast_rerun()
        with action_col2:
            if st.button("Надати доступ", key="admin_approve_access"):
                lexdb.set_user_permission(int(selected_id), "editor", True)
                st.success("Доступ надано.")
                fast_rerun()
        with action_col3:
            if selected_user.get("EMAIL") != DEFAULT_ADMIN_EMAIL and st.button("Відхилити запит", key="admin_reject_access"):
                lexdb.delete_user(int(selected_id))
                st.warning("Запит відхилено, користувача вилучено.")
                fast_rerun()


def render_dictionary_options_admin_panel(current_user: dict) -> None:
    """Адмінське керування випадними списками вкладки «Словник»."""
    if current_user.get("ROLE") != "admin":
        return

    with st.expander("Керування випадними списками словника", expanded=False):
        st.caption(
            "Тут можна вилучити зайві значення з випадних списків. "
            "Вилучення не змінює вже збережені словникові статті, "
            "а лише прибирає значення зі списку для майбутнього вибору."
        )

        field_options = {
            "Частина мови": "ЧАСТИНА МОВИ",
            "Граматичні ознаки — загальні": "ГРАМАТИЧНІ ОЗНАКИ",
            "Граматичні ознаки — іменник": "ГРАМАТИЧНІ ОЗНАКИ::іменник",
            "Граматичні ознаки — прикметник": "ГРАМАТИЧНІ ОЗНАКИ::прикметник",
            "Граматичні ознаки — дієслово": "ГРАМАТИЧНІ ОЗНАКИ::дієслово",
            "Граматичні ознаки — займенник": "ГРАМАТИЧНІ ОЗНАКИ::займенник",
            "Граматичні ознаки — числівник": "ГРАМАТИЧНІ ОЗНАКИ::числівник",
            "Граматичні ознаки — прислівник": "ГРАМАТИЧНІ ОЗНАКИ::прислівник",
            "Стилістичні ремарки": "СТИЛІСТИКА",
            "Типи сталих сполук": "ТИП СТІЙКОЇ СПОЛУКИ",
        }

        selected_label = st.selectbox(
            "Який список редагувати",
            options=list(field_options.keys()),
            key="dict_options_admin_field_label",
        )
        field_name = field_options[selected_label]
        values = cached_lexdb_get_options(field_name)

        if not values:
            st.info("У цьому списку поки немає значень.")
            return

        value_to_delete = st.selectbox(
            "Значення, яке треба вилучити зі списку",
            options=values,
            key=f"dict_options_admin_delete_value_{field_name}",
        )

        st.warning(
            f"Буде вилучено тільки значення «{value_to_delete}» зі списку «{selected_label}». "
            "У вже збережених статтях це значення не буде стерто."
        )

        confirm_delete = st.checkbox(
            "Підтверджую вилучення цього значення зі списку",
            key=f"dict_options_admin_confirm_{field_name}_{value_to_delete}",
        )

        if st.button(
            "Вилучити значення зі списку",
            key=f"dict_options_admin_delete_button_{field_name}_{value_to_delete}",
        ):
            if not confirm_delete:
                st.error("Спершу поставте підтвердження.")
            else:
                lexdb.delete_option(field_name, value_to_delete)
                st.success(f"Значення «{value_to_delete}» вилучено зі списку.")
                fast_rerun()


def context_row_to_quote(row: pd.Series) -> str:
    left = str(row.get("left_context", "") or "").strip()
    key = str(row.get("keyword", "") or "").strip()
    right = str(row.get("right_context", "") or "").strip()
    parts = [p for p in [left, key, right] if p and p != EMPTY_CONTEXT]
    return " ".join(parts).strip()


def highlight_terms(text: str, terms: list[str]) -> str:
    escaped = html.escape(str(text or ""))
    for term in sorted({t for t in terms if t}, key=len, reverse=True):
        pattern = re.compile(re.escape(html.escape(term)), flags=re.IGNORECASE)
        escaped = pattern.sub(lambda m: f"<strong>{m.group(0)}</strong>", escaped)
    return escaped


def select_wordform_ui(wf_alpha: pd.DataFrame, freq_map: dict, key_suffix: str) -> tuple[str | None, int]:
    wf_search = st.text_input("Пошук словоформи в корпусі", placeholder="Наприклад: боротьба", key=f"dict_wf_search_{key_suffix}")
    wf_view = wf_alpha
    if wf_search:
        q = normalize_wordform(wf_search)
        wf_view = wf_view[wf_view["wordform"].str.contains(re.escape(q), regex=True, na=False)]
    if wf_view.empty:
        st.warning("Словоформ не знайдено.")
        return None, 0
    options = wf_view["wordform"].tolist()[:2000]
    selected_wordform = st.selectbox(
        "Вибрати словоформу",
        options=options,
        format_func=lambda w: f"{w} ({int(freq_map.get(w, 0))})",
        key=f"dict_wordform_select_{key_suffix}",
    )
    st.caption(f"Показано {len(options)} словоформ із {len(wf_view)} знайдених.")
    return selected_wordform, int(freq_map.get(selected_wordform, 0))


def render_context_selector(ctx_df: pd.DataFrame, selected_wordform: str) -> pd.DataFrame:
    selected_indices: list[int] = []
    st.markdown(
        "<div class='dict-context-list-title'>Позначте контексти, які треба внести до ілюстративної зони.</div>",
        unsafe_allow_html=True,
    )
    df = ctx_df.reset_index(drop=True).copy()
    if df.empty:
        return pd.DataFrame()

    group_cols = ["article_id", "code", "article_title"]
    for (_, code, title), group in df.groupby(group_cols, sort=False, dropna=False):
        st.markdown(
            f"<div class='dict-source-header'>{html.escape(str(code or ''))} · {html.escape(str(title or ''))}</div>",
            unsafe_allow_html=True,
        )
        for idx, row in group.iterrows():
            cb_col, txt_col = st.columns([0.13, 5.8], vertical_alignment="top")
            unique_key = f"dict_quote_checkbox_{selected_wordform}_{int(row.get('article_id', 0) or 0)}_{int(row.get('token_index', idx) or idx)}_{idx}"
            with cb_col:
                checked = st.checkbox(" ", key=unique_key, label_visibility="collapsed")
            with txt_col:
                st.markdown(
                    "<div class='dict-kwic-row'>"
                    f"<div class='dict-kwic-left'>{html.escape(str(row.get('left_context', '') or EMPTY_CONTEXT))}</div>"
                    f"<div class='dict-kwic-keyword'><strong>{html.escape(str(row.get('keyword', '') or ''))}</strong></div>"
                    f"<div class='dict-kwic-right'>{html.escape(str(row.get('right_context', '') or EMPTY_CONTEXT))}</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )
            if checked:
                selected_indices.append(idx)
    return df.iloc[selected_indices].copy() if selected_indices else pd.DataFrame()


def dict_zone_header(n: int, title: str, subtitle: str = "") -> None:
    st.markdown(
        f"""
        <div class='dict-zone-heading'>
            <div class='dict-zone-number'>{n}</div>
            <div>
                <h3>{html.escape(title)}</h3>
                {f"<p>{html.escape(subtitle)}</p>" if subtitle else ""}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def reference_select(label: str, table: str, key: str, default_id: int | None = None) -> int | None:
    refs = cached_lexdb_list_references(table)
    options = [0] + [int(r["ID"]) for r in refs]
    labels = {int(r["ID"]): f"{r.get('СКОРОЧЕННЯ') or '—'} — {r.get('ПОВНА НАЗВА') or '—'}" for r in refs}
    index = options.index(int(default_id)) if default_id and int(default_id) in options else 0
    selected = st.selectbox(label, options=options, index=index, format_func=lambda x: "— не вибрано —" if x == 0 else labels.get(int(x), str(x)), key=key)
    return int(selected) if selected else None


def word_id_select(label: str, all_words: list[dict], key: str, default_id: int | None = None) -> int | None:
    options = [0] + [int(w["ID"]) for w in all_words]
    labels = {int(w["ID"]): str(w.get("РЕЄСТРОВА ОДИНИЦЯ") or w.get("ID")) for w in all_words}
    index = options.index(int(default_id)) if default_id and int(default_id) in options else 0
    selected = st.selectbox(label, options=options, index=index, format_func=lambda x: "— не вибрано —" if x == 0 else labels.get(int(x), str(x)), key=key)
    return int(selected) if selected else None


def has_marked_stress(value: str) -> bool:
    value = str(value or "")
    return any(ch in "АЕЄИІЇОУЮЯ" for ch in value)


def syllable_count(value: str) -> int:
    return sum(1 for ch in str(value or "") if ch in VOWELS_UA)


def format_source_label(q: dict) -> str:
    abbr = str(q.get("ДЖЕРЕЛО_СКОРОЧЕННЯ") or q.get("СКОРОЧЕННЯ") or "").strip()
    return abbr or "джерело не вказано"



def display_stressed_headword(stressed_form: str, fallback: str = "") -> str:
    """Перетворює форму типу боротьбА на БОРОТЬБА́."""
    raw = str(stressed_form or "").strip() or str(fallback or "").strip()
    out: list[str] = []
    stressed_done = False
    for ch in raw:
        if ch in "АЕЄИІЇОУЮЯ" and not stressed_done:
            out.append(ch.upper() + "\u0301")
            stressed_done = True
        else:
            out.append(ch.upper())
    return "".join(out)


def html_join_semicolon(values: list[str]) -> str:
    return "; ".join(v for v in values if str(v or "").strip())


def split_semicolon_values(raw: str) -> list[str]:
    return [p.strip() for p in str(raw or "").split(";") if p.strip()]


def definition_number_map(definitions: list[dict]) -> dict[int, int]:
    return {int(d.get("ID")): idx for idx, d in enumerate(definitions, start=1) if d.get("ID")}


def quote_sources_for_wordform(wf: dict) -> str:
    sources = sorted({format_source_label(q) for q in wf.get("quotes", []) or [] if format_source_label(q) != "джерело не вказано"})
    return "[" + ", ".join(sources) + "]" if sources else "[…]"


def highlight_entry_terms_html(text_value: str, article: dict) -> str:
    word = article.get("word") or {}
    terms = [str(word.get("РЕЄСТРОВА ОДИНИЦЯ") or "").strip()]
    for wf in article.get("wordforms", []) or []:
        terms.append(str(wf.get("СЛОВОФОРМА") or "").strip())
    for variant in article.get("variants", []) or []:
        terms.append(str(variant.get("ВАРІЯНТ") or "").strip())
        terms.append(str(variant.get("НАГОЛОШЕНА ФОРМА") or "").strip())
        for variant_wf in variant.get("wordforms", []) or []:
            terms.append(str(variant_wf.get("СЛОВОФОРМА") or "").strip())
    escaped = html.escape(str(text_value or ""))
    for term in sorted({t for t in terms if t}, key=len, reverse=True):
        pattern = re.compile(re.escape(html.escape(term)), flags=re.IGNORECASE)
        escaped = pattern.sub(lambda m: f"<strong class='dict-hit'>{m.group(0)}</strong>", escaped)
    return escaped


def format_grammar_zone_html(article: dict, definition_numbers: dict[int, int]) -> str:
    wordforms = article.get("wordforms") or []
    if not wordforms:
        return ""
    groups: dict[str, list[dict]] = {}
    for wf in wordforms:
        grammar = str(wf.get("ГРАМАТИЧНІ ОЗНАКИ") or "грам. ознаки не вказано").strip()
        groups.setdefault(grammar, []).append(wf)
    group_html: list[str] = []
    for grammar, items in groups.items():
        wf_parts: list[str] = []
        for wf in items:
            form = html.escape(str(wf.get("СЛОВОФОРМА") or ""))
            def_id = safe_int(wf.get("ТЛУМАЧЕННЯ_ID"), 0) or safe_int(wf.get("ЗНАЧЕННЯ_ID"), 0)
            meaning_num = definition_numbers.get(def_id)
            meaning_part = f" {meaning_num}." if meaning_num else ""
            freq_part = f" ({safe_int(wf.get('ЧАСТОТА'), 0)})"
            sources_part = quote_sources_for_wordform(wf)
            styl = str(wf.get("СТИЛІСТИКА") or "").strip()
            styl_part = f" <em>{html.escape(styl)}</em>" if styl else ""
            wf_parts.append(f"<strong>{form}</strong>{meaning_part}{freq_part}{styl_part} {html.escape(sources_part)}")
        group_html.append(f"<strong><em>{html.escape(grammar)}</em></strong> " + ", ".join(wf_parts))
    return "<div class='dict-grammar-zone'><span class='dict-symbol'>•</span> " + "; ".join(group_html) + ".</div>"


def format_ready_dictionary_entry_html(article: dict, font_family: str = "Georgia, Cambria, 'Times New Roman', serif", font_size: int = 18, compact: bool = False) -> str:
    word = article.get("word") or {}
    if not word:
        return ""
    register = str(word.get("РЕЄСТРОВА ОДИНИЦЯ") or "").strip()
    head = display_stressed_headword(str(word.get("НАГОЛОШЕНА ФОРМА") or ""), register)
    total_freq = safe_int(word.get("ЧАСТОТА"), 0)
    variants = article.get("variants") or []
    definitions = article.get("definitions") or []
    definition_numbers = definition_number_map(definitions)

    variant_html: list[str] = []
    variant_frequency_sum = 0
    for v in variants:
        label = display_stressed_headword(str(v.get("НАГОЛОШЕНА ФОРМА") or v.get("ВАРІЯНТ") or ""), str(v.get("ВАРІЯНТ") or ""))
        freq = safe_int(v.get("ЧАСТОТА"), 0)
        variant_frequency_sum += freq
        if label:
            variant_html.append(f"<span class='dict-variant'>{html.escape(label)}{f' ({freq})' if freq else ''}</span>")

    pos = str(word.get("ЧАСТИНА МОВИ") or "").strip()
    grammar = str(word.get("ГРАМАТИЧНІ ОЗНАКИ") or "").strip()
    styl = str(word.get("СТИЛІСТИКА") or "").strip()
    grammar_line = html_join_semicolon([pos, grammar, styl])
    origin = str(word.get("ПОХОДЖЕННЯ") or "").strip()

    if variant_html and total_freq >= variant_frequency_sum:
        head_frequency = total_freq - variant_frequency_sum
    else:
        head_frequency = total_freq

    if variant_html:
        head_block = (
            f"<strong>{html.escape(head)}</strong> <span class='dict-total'>({head_frequency})</span> "
            "/ " + " / ".join(variant_html) + f" <span class='dict-total-separator'>|</span> <span class='dict-total'>({total_freq})</span>."
        )
    else:
        head_block = f"<strong>{html.escape(head)}</strong> <span class='dict-total'>({total_freq})</span>."

    parts: list[str] = []
    parts.append(
        "<div class='dict-entry-head'>"
        + head_block
        + (" " + f"<em>{html.escape(grammar_line)}</em>" if grammar_line else "")
        + (f" <span class='dict-etym'>‹{html.escape(origin)}›</span>" if origin else "")
        + "</div>"
    )

    for i, d in enumerate(definitions, start=1):
        d_freq = safe_int(d.get("ЧАСТОТА"), 0)
        d_styl = str(d.get("СТИЛІСТИКА") or "").strip()
        d_text = str(d.get("ТЛУМАЧЕННЯ") or "").strip()
        rem = f" <em>{html.escape(d_styl)}</em>" if d_styl else ""
        parts.append(f"<div class='dict-def-line'><strong>{i}. ({d_freq})</strong>{rem} {html.escape(d_text)}</div>")
        for sub in d.get("subdefinitions", []) or []:
            sub_txt = str(sub.get("ТЕКСТ") or "").strip()
            if sub_txt:
                sub_freq = safe_int(sub.get("ЧАСТОТА"), 0)
                sub_styl = str(sub.get("СТИЛІСТИКА") or "").strip()
                sub_meta = f"({sub_freq}) " if sub_freq else ""
                sub_rem = f"<em>{html.escape(sub_styl)}</em> " if sub_styl else ""
                parts.append(f"<div class='dict-subdef'>// {sub_meta}{sub_rem}{html.escape(sub_txt)}</div>")
        for q in d.get("quotes", []) or []:
            first = str(q.get("ПЕРШОДРУК") or "").strip()
            if first:
                parts.append(f"<div class='dict-illustration'>{highlight_entry_terms_html(first, article)} <span class='dict-source'>[{html.escape(format_source_label(q))}]</span></div>")
            reprint = str(q.get("ПЕРЕДРУК") or "").strip()
            if reprint:
                parts.append(f"<div class='dict-reprint'><span class='dict-symbol'>↕</span> {highlight_entry_terms_html(reprint, article)} <span class='dict-source'>[ПУР-1978].</span></div>")

    rels = article.get("semantic_relations") or []
    syns = [str(r.get("ОДИНИЦЯ") or "").strip() for r in rels if str(r.get("ТИП") or "") == "синонім" and str(r.get("ОДИНИЦЯ") or "").strip()]
    ants = [str(r.get("ОДИНИЦЯ") or "").strip() for r in rels if str(r.get("ТИП") or "") == "антонім" and str(r.get("ОДИНИЦЯ") or "").strip()]
    if syns:
        parts.append("<div class='dict-rel'><span class='dict-symbol'>=</span> " + html.escape("; ".join(syns)) + ".</div>")
    if ants:
        parts.append("<div class='dict-rel'><span class='dict-symbol'>≠</span> " + html.escape("; ".join(ants)) + ".</div>")

    collocations = article.get("collocations") or []
    if collocations:
        parts.append("<div class='dict-zone-label'><span class='dict-symbol'>♦</span> Фразеологічно-сполучувальна зона</div>")
        for c in collocations:
            c_unit = str(c.get("ОДИНИЦЯ") or "").strip()
            c_meta = html_join_semicolon([str(c.get("ТИП") or "").strip(), f"частота {safe_int(c.get('ЧАСТОТА'), 0)}" if safe_int(c.get("ЧАСТОТА"), 0) else "", str(c.get("СТИЛІСТИКА") or "").strip()])
            c_def = str(c.get("ТЛУМАЧЕННЯ") or "").strip()
            parts.append(f"<div class='dict-coll'><strong>{html.escape(c_unit)}</strong>{f' <em>({html.escape(c_meta)})</em>' if c_meta else ''}{' — ' + html.escape(c_def) if c_def else ''}</div>")
            for q in c.get("quotes", []) or []:
                first = str(q.get("ПЕРШОДРУК") or "").strip()
                if first:
                    parts.append(f"<div class='dict-illustration'>{highlight_entry_terms_html(first, article)} <span class='dict-source'>[{html.escape(format_source_label(q))}]</span></div>")
                reprint = str(q.get("ПЕРЕДРУК") or "").strip()
                if reprint:
                    parts.append(f"<div class='dict-reprint'><span class='dict-symbol'>↕</span> {highlight_entry_terms_html(reprint, article)} <span class='dict-source'>[ПУР-1978].</span></div>")

    grammar_zone = format_grammar_zone_html(article, definition_numbers)
    if grammar_zone:
        parts.append(grammar_zone)

    crossrefs = article.get("crossrefs") or []
    refs = [str(r.get("ТЕКСТ") or "").strip() for r in crossrefs if str(r.get("ТЕКСТ") or "").strip()]
    if refs:
        parts.append("<div class='dict-crossref'><span class='dict-symbol'>→</span> " + html.escape("; ".join(refs)) + ".</div>")

    spacing_class = " compact" if compact else ""
    return f"<div class='dict-preview-card{spacing_class}' style=\"font-family:{html.escape(font_family)}; font-size:{int(font_size)}px;\">" + "\n".join(parts) + "</div>"


def format_ready_dictionary_entry(article: dict) -> str:
    html_text = format_ready_dictionary_entry_html(article)
    html_text = re.sub(r"<br\s*/?>", "\n", html_text)
    html_text = re.sub(r"</div>\s*<div", "\n<div", html_text)
    html_text = re.sub(r"<[^>]+>", "", html_text)
    return html.unescape(html_text).strip()


def validate_dictionary_article(article: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    word = article.get("word") or {}
    if not word:
        errors.append("Статтю ще не створено: спершу збережіть реєстрову одиницю.")
        return errors, warnings
    register = str(word.get("РЕЄСТРОВА ОДИНИЦЯ") or "").strip()
    stressed = str(word.get("НАГОЛОШЕНА ФОРМА") or "").strip()
    if not register:
        errors.append("Не заповнено реєстрову одиницю.")
    if syllable_count(register) > 1 and not has_marked_stress(stressed):
        warnings.append("Для понадодноскладового слова бажано подати наголошену форму: напр., боротьбА.")
    if not word.get("ЧАСТИНА МОВИ"):
        errors.append("Не вказано частину мови.")
    if not word.get("ГРАМАТИЧНІ ОЗНАКИ"):
        warnings.append("Не заповнено граматичні ознаки леми.")
    definitions = article.get("definitions") or []
    if not definitions:
        errors.append("Немає жодного тлумачення / функційного пояснення.")
    else:
        for i, d in enumerate(definitions, start=1):
            if not str(d.get("ТЛУМАЧЕННЯ") or "").strip():
                errors.append(f"Значення {i} не має тексту тлумачення.")
            if not (d.get("quotes") or []):
                warnings.append(f"Значення {i} не має прикріпленої ілюстрації.")
    all_quotes = []
    for d in definitions:
        all_quotes.extend(d.get("quotes") or [])
    for c in article.get("collocations") or []:
        all_quotes.extend(c.get("quotes") or [])
    for q in all_quotes:
        if not str(q.get("ПЕРШОДРУК") or "").strip():
            errors.append(f"Цитата ID {q.get('ID')} не має тексту першодруку.")
        if not (q.get("ДЖЕРЕЛО_ID") or q.get("ДЖЕРЕЛО_СКОРОЧЕННЯ")):
            warnings.append(f"Цитата ID {q.get('ID')} не має джерельної паспортизації.")
    return errors, warnings


def render_dictionary_css() -> None:
    st.markdown(
        """
        <style>
        .st-key-dict_full_editor {
            max-width: 1120px !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }
        .dict-compiler-guide,
        div[class*="st-key-dict_zone_"] {
            width: min(100%, 1080px);
            margin: 1.05rem auto;
            padding: 1.25rem 1.35rem;
            border-radius: 22px;
            background: linear-gradient(180deg, rgba(255,252,244,0.98), rgba(255,246,233,0.95));
            border: 1px solid rgba(241,198,90,0.42);
            box-shadow: 0 18px 38px rgba(0,0,0,0.20), 0 0 0 1px rgba(255,255,255,0.70) inset;
            color: #24171f !important;
        }
        div[class*="st-key-dict_zone_"] * { color: #24171f !important; }
        div[class*="st-key-dict_zone_"] button *,
        div[class*="st-key-dict_zone_"] div[data-testid="stFormSubmitButton"] button * { color: #ffffff !important; }
        .dict-compiler-guide * { color: #24171f !important; }
        .dict-compiler-guide h3 {
            text-align: center;
            margin: 0 0 0.75rem 0;
            color: #76001f !important;
            font-size: 1.42rem;
            font-weight: 950;
        }
        .dict-compiler-guide p, .dict-compiler-guide li {
            font-size: 1.01rem;
            line-height: 1.58;
        }
        .dict-guide-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 0.62rem 0.72rem;
            margin-top: 0.9rem;
        }
        .dict-guide-item {
            padding: 0.72rem 0.82rem;
            border-radius: 14px;
            background: rgba(255,255,255,0.72);
            border: 1px solid rgba(176,112,44,0.22);
            line-height: 1.46;
            font-size: 0.96rem;
        }
        .dict-guide-title {
            color: #76001f !important;
            font-weight: 950;
        }
        .dict-download-spacer { height: 0.95rem; }
        .dict-zone-heading {
            width: min(100%, 1080px);
            margin: 1.45rem auto 0.65rem auto;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.85rem;
            text-align: left;
        }
        .dict-zone-number {
            width: 42px;
            height: 42px;
            border-radius: 999px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 950;
            color: #fff8ec;
            background: radial-gradient(circle at 35% 28%, #e7bb55, #76001f 62%, #3a000d 100%);
            box-shadow: 0 0 18px rgba(241,198,90,0.36);
            flex: 0 0 auto;
        }
        .dict-zone-heading h3 {
            margin: 0;
            color: #fff8ec !important;
            font-size: 1.48rem;
            font-weight: 950;
            letter-spacing: -0.01em;
        }
        .dict-zone-heading p {
            margin: 0.16rem 0 0 0;
            color: rgba(255,248,236,0.82) !important;
            font-size: 0.98rem;
        }
        .dict-section-note {
            color: #5e4651 !important;
            font-size: 0.96rem;
            line-height: 1.5;
            margin: 0.2rem 0 0.8rem 0;
        }
        .dict-preview-card {
            white-space: normal;
            font-family: Georgia, Cambria, 'Times New Roman', serif;
            font-size: 1.08rem;
            line-height: 1.64;
            color: #24171f !important;
            background: rgba(255,253,248,0.98);
            border-left: 6px solid #76001f;
            border-radius: 16px;
            padding: 1rem 1.15rem;
            box-shadow: 0 10px 24px rgba(0,0,0,0.08);
        }
        .dict-preview-card.compact { line-height: 1.38; }
        .dict-entry-head { margin-bottom: 0.72rem; }
        .dict-entry-head strong { color: #76001f !important; font-size: 1.34em; letter-spacing: 0.035em; }
        .dict-total { color: #24171f !important; font-weight: 850; }
        .dict-variant { color: #5a3b25 !important; font-weight: 800; }
        .dict-total-separator { color: #76001f !important; font-weight: 950; padding: 0 0.22rem; }
        .dict-symbol {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 1.55em;
            height: 1.55em;
            padding: 0 0.28em;
            margin-right: 0.28em;
            border-radius: 0.48em;
            border: 1.5px solid rgba(118, 0, 31, 0.34);
            background: linear-gradient(180deg, rgba(118,0,31,0.12), rgba(241,198,90,0.18));
            color: #76001f !important;
            font-weight: 950;
            line-height: 1;
            box-shadow: 0 3px 8px rgba(118,0,31,0.10);
        }
        .dict-etym { color: #5f4a53 !important; font-style: italic; }
        .dict-def-line { margin: 0.52rem 0 0.2rem 0; }
        .dict-def-line em, .dict-subdef em { color: #5f4a53 !important; font-style: italic; }
        .dict-subdef { margin-left: 1.15rem; color: #3c2a34 !important; }
        .dict-illustration, .dict-reprint { margin: 0.32rem 0 0.32rem 1.15rem; }
        .dict-reprint { color: #5a3b25 !important; }
        .dict-hit { color: #76001f !important; background: rgba(118,0,31,0.09); padding: 0 0.12rem; border-radius: 0.22rem; }
        .dict-source { color: #76001f !important; font-weight: 850; }
        .dict-rel, .dict-crossref, .dict-grammar-zone { margin-top: 0.55rem; }
        .dict-zone-label { margin-top: 0.72rem; font-weight: 950; color: #76001f !important; }
        .dict-coll { margin: 0.38rem 0 0.18rem 1.15rem; }
        .dict-mini-card {
            background: rgba(255,255,255,0.78);
            border: 1px solid rgba(176,112,44,0.24);
            border-radius: 15px;
            padding: 0.85rem 0.95rem;
            margin: 0.55rem 0;
            color: #24171f !important;
        }
        .dict-mini-card * { color: #24171f !important; }
        .dict-badge-row { display:flex; flex-wrap:wrap; justify-content:center; gap:0.45rem; margin:0.6rem 0; }
        .dict-badge { padding:0.32rem 0.64rem; border-radius:999px; background:rgba(118,0,31,0.08); color:#76001f !important; font-weight:850; }
        .dict-context-list-title { color: rgba(255,248,236,0.96) !important; font-weight: 850; margin: 0.75rem 0 0.5rem 0; }
        .dict-source-header { margin: 1rem 0 0.45rem 0; padding: 0.52rem 0.78rem; border-left: 5px solid rgba(241,198,90,0.82); border-radius: 9px; background: rgba(255,248,236,0.12); color: rgba(255,248,236,0.98) !important; font-weight: 900; box-shadow: 0 8px 18px rgba(0,0,0,0.08); }
        .dict-kwic-row { display: grid; grid-template-columns: minmax(0, 42%) minmax(126px, 14%) minmax(0, 42%); gap: 9px; align-items: start; background: rgba(255,250,247,0.96); border: 1px solid rgba(176,112,44,0.20); border-radius: 12px; padding: 0.58rem 0.72rem; margin: 0 0 0.45rem 0; box-shadow: 0 7px 16px rgba(0,0,0,0.08); font-family: Georgia, Cambria, 'Times New Roman', serif; font-size: 1.02rem; line-height: 1.43; }
        .dict-kwic-left { text-align: right; color: #24171f !important; }
        .dict-kwic-keyword { text-align: center; font-weight: 950; color: #76001f !important; background: rgba(118,0,31,0.075); border-radius: 9px; padding: 0 0.36rem; }
        .dict-kwic-right { text-align: left; color: #24171f !important; }
        .st-key-dict_full_editor label p,
        .st-key-dict_full_editor div[data-testid="stTextInput"] label p,
        .st-key-dict_full_editor div[data-testid="stTextArea"] label p,
        .st-key-dict_full_editor div[data-testid="stNumberInput"] label p,
        .st-key-dict_full_editor div[data-testid="stSelectbox"] label p,
        .st-key-dict_full_editor div[data-testid="stMultiSelect"] label p,
        .st-key-dict_full_editor div[data-testid="stCheckbox"] label p {
            color: rgba(255,248,236,0.96) !important;
            font-weight: 850 !important;
        }
        div[class*="st-key-dict_zone_"] label p,
        div[class*="st-key-dict_zone_"] div[data-testid="stTextInput"] label p,
        div[class*="st-key-dict_zone_"] div[data-testid="stTextArea"] label p,
        div[class*="st-key-dict_zone_"] div[data-testid="stNumberInput"] label p,
        div[class*="st-key-dict_zone_"] div[data-testid="stSelectbox"] label p,
        div[class*="st-key-dict_zone_"] div[data-testid="stMultiSelect"] label p,
        div[class*="st-key-dict_zone_"] div[data-testid="stCheckbox"] label p {
            color: #24171f !important;
        }
        .st-key-dict_lower_tabs div[data-baseweb="tab-list"] {
            position: static !important;
            top: auto !important;
            width: min(100%, 1120px) !important;
            margin: 0.15rem auto 1.1rem auto !important;
            padding: 0.35rem !important;
            gap: 0.45rem !important;
            border: 1px solid rgba(241,198,90,0.42) !important;
            border-radius: 18px !important;
            background: rgba(255,248,236,0.12) !important;
            box-shadow: 0 12px 26px rgba(0,0,0,0.14) !important;
            backdrop-filter: blur(4px);
        }
        .st-key-dict_lower_tabs button[role="tab"] {
            border-radius: 13px !important;
            border: 1px solid rgba(241,198,90,0.32) !important;
            border-bottom: 1px solid rgba(241,198,90,0.32) !important;
            background: linear-gradient(180deg, rgba(255,250,241,0.96), rgba(255,238,214,0.92)) !important;
            color: #5a3b25 !important;
            min-height: 50px !important;
            padding: 0.55rem 1.35rem !important;
            box-shadow: 0 7px 16px rgba(0,0,0,0.11) !important;
            transform: none !important;
        }
        .st-key-dict_lower_tabs button[role="tab"] p {
            flex-direction: row !important;
            gap: 0.46rem !important;
            font-size: 1.02rem !important;
            font-weight: 900 !important;
            color: inherit !important;
        }
        .st-key-dict_lower_tabs button[role="tab"] p::before { display: none !important; content: none !important; }
        .st-key-dict_lower_tabs button[role="tab"][aria-selected="true"] {
            background: linear-gradient(180deg, #9f123a 0%, #76001f 100%) !important;
            color: #fff8ec !important;
            border-color: rgba(241,198,90,0.78) !important;
            box-shadow: 0 12px 24px rgba(0,0,0,0.20), 0 0 0 1px rgba(255,255,255,0.12) inset !important;
        }
        .st-key-dict_lower_tabs div.stButton > button,
        .st-key-dict_lower_tabs div.stButton > button *,
        .st-key-dict_lower_tabs div[data-testid="stFormSubmitButton"] button,
        .st-key-dict_lower_tabs div[data-testid="stFormSubmitButton"] button * {
            color: #ffffff !important;
        }
        .st-key-dict_lower_tabs div[data-testid="stDownloadButton"] {
            margin-top: 0.85rem !important;
            display: inline-block !important;
        }
        .st-key-dict_lower_tabs div[data-testid="stDownloadButton"] button,
        .st-key-dict_lower_tabs div[data-testid="stDownloadButton"] button * {
            color: #24171f !important;
        }
        .st-key-dict_lower_tabs div[class*="st-key-dict_view_font_size_"] label p,
        .st-key-dict_lower_tabs div[class*="st-key-dict_view_font_size_"] div,
        .st-key-dict_lower_tabs div[class*="st-key-dict_view_font_size_"] span,
        .st-key-dict_lower_tabs div[class*="st-key-dict_view_font_size_"] p {
            color: #fff8ec !important;
        }
        @media (max-width: 820px) {
            .dict-zone-heading { align-items:flex-start; justify-content:flex-start; }
            .dict-kwic-row { grid-template-columns: 1fr !important; }
            .dict-kwic-left, .dict-kwic-keyword, .dict-kwic-right { text-align: left !important; }
            .dict-guide-grid { grid-template-columns: 1fr !important; }
            .st-key-dict_lower_tabs div[data-baseweb="tab-list"] { display: grid !important; grid-template-columns: 1fr 1fr !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_zone_box_start() -> None:
    return None


def render_zone_box_end() -> None:
    return None


def render_dictionary_tab(wordforms_df: pd.DataFrame) -> None:
    render_dictionary_css()
    st.subheader("Словник мови Степана Бандери")
    st.caption("Повний редактор словникової статті: від корпусної словоформи до готової лексикографічної статті.")

    user = render_dictionary_auth()
    if user is None:
        return
    render_dictionary_admin_panel(user)
    render_dictionary_options_admin_panel(user)

    with keyed_container("dict_full_editor"):
        st.markdown(
            """
            <div class='dict-compiler-guide'>
            <h3>Інструкція для укладача</h3>
            <p><strong>Працюйте послідовно згори донизу.</strong> Кожна зона відповідає окремому фрагментові майбутньої словникової статті. Спершу виберіть словоформу або відкрийте вже створену статтю, далі збережіть реєстрову й граматичну зону, після цього додавайте варіянти, значення, ілюстрації, сталі сполуки, синоніми, антоніми та відсилання.</p>
            <div class='dict-guide-grid'>
                <div class='dict-guide-item'><span class='dict-guide-title'>1. Навігація.</span> Оберіть корпусну словоформу або вже створену статтю. Це визначає, з яким матеріалом працює редактор.</div>
                <div class='dict-guide-item'><span class='dict-guide-title'>2. Реєстрова зона.</span> Уведіть основну лему й наголошену форму. Наголос позначайте великою голосною: <em>боротьбА, УкраїнА, революцІя</em>.</div>
                <div class='dict-guide-item'><span class='dict-guide-title'>3. Граматика й стилістика.</span> Заповніть частину мови, граматичні ознаки, стилістичні ремарки та етимологічну довідку. Позначку <strong>‹…›</strong> система поставить сама.</div>
                <div class='dict-guide-item'><span class='dict-guide-title'>4. Варіянти.</span> Додавайте тільки лексикографічно значущі варіянти. Якщо варіянт має словоформи, зводьте їх до цього варіянта: частота підсумується автоматично.</div>
                <div class='dict-guide-item'><span class='dict-guide-title'>5. Словоформи.</span> Однакова словоформа може мати різні граматичні, стилістичні й семантичні параметри, тому за потреби створюйте окремі записи.</div>
                <div class='dict-guide-item'><span class='dict-guide-title'>6. Значення.</span> Формулюйте тлумачення або функційне пояснення. Частота значення визначається кількістю прикріплених ілюстрацій.</div>
                <div class='dict-guide-item'><span class='dict-guide-title'>7. Ілюстрації.</span> Добирайте контексти з конкордансу, редагуйте їх перед збереженням, скорочення позначайте як <strong>[…]</strong>, джерело першодруку вибирайте зі списку.</div>
                <div class='dict-guide-item'><span class='dict-guide-title'>8. ПУР-1978.</span> Якщо є редакційна відмінність, внесіть її в поле «Передрук ПУР-1978» — у статті автоматично з’явиться зона <strong>↕</strong>.</div>
                <div class='dict-guide-item'><span class='dict-guide-title'>9. Сталі сполуки.</span> Уносіть фразеологізми, політичні формули й термінологізовані сполуки; для них також можна додавати ілюстрації.</div>
                <div class='dict-guide-item'><span class='dict-guide-title'>10. Перегляд.</span> Символи <strong>/</strong>, <strong>→</strong>, <strong>♦</strong>, <strong>•</strong>, <strong>=</strong>, <strong>≠</strong> уводити вручну не треба: готова стаття формується автоматично.</div>
            </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        wf_alpha = wordforms_df[["wordform", "frequency"]].dropna().copy()
        wf_alpha["wordform"] = wf_alpha["wordform"].astype(str)
        wf_alpha = wf_alpha.sort_values("wordform", key=lambda s: s.str.casefold())
        freq_map = dict(zip(wf_alpha["wordform"], wf_alpha["frequency"]))

        all_words = cached_lexdb_search_words("", limit=20000)
        selected_existing = 0
        selected_wordform = None
        selected_frequency = 0

        dict_zone_header(1, "Навігація й вибір матеріалу", "Виберіть корпусну словоформу або відкрийте вже створену словникову статтю.")
        nav_box = keyed_container("dict_zone_navigation")
        with nav_box:
            render_zone_box_start()
            nav1, nav2 = st.columns([1.15, 1.15], gap="large")
            with nav1:
                selected_wordform, selected_frequency = select_wordform_ui(wf_alpha, freq_map, "full_editor")
                if selected_wordform:
                    st.session_state["dict_current_wordform"] = selected_wordform
            with nav2:
                existing_query = st.text_input("Пошук у створених статтях", placeholder="Наприклад: боротьба", key="dict_existing_search_full")
                existing_words = cached_lexdb_search_words(existing_query, limit=600)
                if existing_words:
                    selected_existing = st.selectbox(
                        "Відкрити створену статтю",
                        options=[0] + [int(w["ID"]) for w in existing_words],
                        format_func=lambda x: "— не вибрано —" if x == 0 else next((str(w["РЕЄСТРОВА ОДИНИЦЯ"]) for w in existing_words if int(w["ID"]) == int(x)), str(x)),
                        key="dict_existing_word_select_full",
                    )
                else:
                    st.info("Створених статей за цим фільтром не знайдено.")
            render_zone_box_end()

        current_word_id = int(selected_existing or 0) or None
        article = cached_lexdb_get_dictionary_article_full(current_word_id) if current_word_id else {"word": None, "wordforms": [], "definitions": [], "collocations": [], "variants": [], "crossrefs": [], "semantic_relations": []}
        current_word = article.get("word") or {}
        if current_word_id and not selected_wordform:
            wfs = article.get("wordforms") or []
            if wfs:
                selected_wordform = str(wfs[0].get("СЛОВОФОРМА") or "")
                selected_frequency = safe_int(wfs[0].get("ЧАСТОТА"), 0)
            else:
                selected_wordform = str(current_word.get("РЕЄСТРОВА ОДИНИЦЯ") or "")
                selected_frequency = safe_int(current_word.get("ЧАСТОТА"), 0)
        if not selected_wordform and st.session_state.get("dict_current_wordform"):
            selected_wordform = st.session_state.get("dict_current_wordform")
            selected_frequency = safe_int(freq_map.get(selected_wordform), 0)

        if not selected_wordform and not current_word_id:
            st.info("Оберіть словоформу або відкрийте вже створену статтю.")
            return

        default_register = str(current_word.get("РЕЄСТРОВА ОДИНИЦЯ") or selected_wordform or "")
        default_stressed = str(current_word.get("НАГОЛОШЕНА ФОРМА") or "")
        default_pos = str(current_word.get("ЧАСТИНА МОВИ") or "")
        default_grammar = str(current_word.get("ГРАМАТИЧНІ ОЗНАКИ") or "")
        default_stylistics = str(current_word.get("СТИЛІСТИКА") or "")
        default_origin = str(current_word.get("ПОХОДЖЕННЯ") or "")

        dict_zone_header(2, "Реєстрова зона", "Заповнює таблицю СЛОВО й задає початок майбутньої словникової статті.")
        reg_box = keyed_container("dict_zone_register")
        with reg_box:
            render_zone_box_start()
            r1, r2 = st.columns([1.2, 1.2])
            with r1:
                register = st.text_input("Реєстрова одиниця", value=default_register, key=f"dict_register_full_{current_word_id or 'new'}_{selected_wordform}")
            with r2:
                stressed_form = st.text_input("Наголошена форма", value=default_stressed, placeholder="Напр.: боротьбА", key=f"dict_stressed_full_{current_word_id or 'new'}_{selected_wordform}")
            m1, m2, m3 = st.columns(3)
            with m1:
                st.metric("Частота вибраної словоформи", selected_frequency)
            with m2:
                st.metric("Частота леми", safe_int(current_word.get("ЧАСТОТА"), 0))
            with m3:
                st.metric("ID статті", current_word_id or "ще не створено")
            if syllable_count(register) > 1 and not has_marked_stress(stressed_form):
                st.warning("Для понадодноскладового слова введіть наголошену форму з великою наголошеною голосною, напр.: боротьбА.")
            render_zone_box_end()

        dict_zone_header(3, "Граматико-стилістична й етимологічна зона", "Заповнює частину мови, граматичні ознаки, стилістичні ремарки й етимологічну довідку ‹…›.")
        gram_box = keyed_container("dict_zone_grammar")
        with gram_box:
            render_zone_box_start()
            g1, g2 = st.columns([0.9, 1.6])
            with g1:
                pos = dictionary_option_select("Частина мови", "ЧАСТИНА МОВИ", default_pos, key_prefix=f"dict_pos_full_{current_word_id or 'new'}_{selected_wordform}")
            with g2:
                lemma_grammar = grammar_multiselect_for_pos("Граматичні ознаки леми", pos, default_grammar, key_prefix=f"dict_lemma_grammar_full_{current_word_id or 'new'}_{selected_wordform}_{pos}")
            lemma_stylistics = dictionary_option_multiselect("Стилістичні / функційні ремарки леми", "СТИЛІСТИКА", default_stylistics, key_prefix=f"dict_lemma_styl_full_{current_word_id or 'new'}_{selected_wordform}")
            origin = st.text_area("Етимологічна довідка / походження / лексикографічний коментар", value=default_origin, height=96, key=f"dict_origin_full_{current_word_id or 'new'}_{selected_wordform}")
            if st.button("Зберегти реєстрову й граматичну зону", type="primary", key=f"dict_save_word_full_{current_word_id or 'new'}_{selected_wordform}"):
                if not register.strip():
                    st.error("Реєстрова одиниця не може бути порожньою.")
                else:
                    current_word_id = lexdb.save_word_full(current_word_id, register, stressed_form, 0, pos, lemma_grammar, lemma_stylistics, origin)
                    lexdb.upsert_wordform(current_word_id, selected_wordform, selected_frequency, pos=pos, grammar="", stylistics="")
                    lexdb.recompute_word_frequency(current_word_id)
                    st.success("Реєстрову й граматичну зону збережено.")
                    fast_rerun()
            render_zone_box_end()

        if not current_word_id:
            st.info("Після збереження реєстрової й граматичної зони відкриються всі инші зони словникової статті.")
            return

        article = cached_lexdb_get_dictionary_article_full(current_word_id)
        current_word = article.get("word") or {}
        all_words = cached_lexdb_search_words("", limit=20000)

        dict_zone_header(4, "Варіянти й відсилання", "Формує зони / та → у готовій словниковій статті.")
        var_box = keyed_container("dict_zone_variants_refs")
        with var_box:
            render_zone_box_start()
            st.markdown("<p class='dict-section-note'>Варіянти вводьте лише тоді, коли вони мають лексикографічне значення. У готовій статті вони будуть подані через /. <strong>Частота варіянта входить до загальної частоти леми.</strong> Якщо до варіянта додати словоформи, частота варіянта автоматично стане сумою частот цих словоформ.</p>", unsafe_allow_html=True)
            v1, v2, v3 = st.columns([1.1, 1.1, 0.7])
            with v1:
                new_variant = st.text_input("Варіянт", placeholder="Напр.: катедра", key=f"dict_variant_value_{current_word_id}")
                new_variant_stressed = st.text_input("Наголошена форма варіянта", placeholder="Напр.: катЕдра", key=f"dict_variant_stressed_{current_word_id}")
            with v2:
                new_variant_type = st.text_input("Тип варіянта", placeholder="правописний, морфологійний, редакційний", key=f"dict_variant_type_{current_word_id}")
                new_variant_comment = st.text_input("Коментар до варіянта", key=f"dict_variant_comment_{current_word_id}")
            with v3:
                new_variant_frequency = st.number_input("Частота варіянта", min_value=0, value=0, step=1, key=f"dict_variant_freq_{current_word_id}")
                st.caption("Якщо словоформ ще немає, це ручна частота. Після додавання словоформ вона перераховується автоматично.")
                if st.button("Додати варіянт", key=f"dict_add_variant_{current_word_id}"):
                    if new_variant.strip():
                        lexdb.upsert_variant(None, current_word_id, new_variant, new_variant_stressed, new_variant_frequency, new_variant_type, new_variant_comment)
                        st.success("Варіянт додано. Загальну частоту леми перераховано.")
                        fast_rerun()
                    else:
                        st.error("Варіянт не може бути порожнім.")

            definitions_for_variants = cached_lexdb_list_definitions(current_word_id)
            variant_def_options = [0] + [int(d["ID"]) for d in definitions_for_variants]
            variant_def_labels = {int(d["ID"]): f"{i+1}. {str(d.get('ТЛУМАЧЕННЯ') or '')[:80]}" for i, d in enumerate(definitions_for_variants)}

            variants = cached_lexdb_list_variants(current_word_id)
            if variants:
                st.markdown(
                    "<div class='dict-badge-row'>" + "".join(
                        f"<span class='dict-badge'>/ {html.escape(str(v.get('НАГОЛОШЕНА ФОРМА') or v.get('ВАРІЯНТ')))} ({safe_int(v.get('ЧАСТОТА'),0)})" +
                        (f" · словоформ: {safe_int(v.get('КІЛЬКІСТЬ_СЛОВОФОРМ'),0)}" if safe_int(v.get('КІЛЬКІСТЬ_СЛОВОФОРМ'),0) else "") +
                        "</span>" for v in variants
                    ) + "</div>",
                    unsafe_allow_html=True,
                )
                with st.expander("Редагувати варіянти й словоформи варіянтів", expanded=False):
                    for v in variants:
                        st.markdown(f"<div class='dict-mini-card'><strong>/ {html.escape(str(v.get('НАГОЛОШЕНА ФОРМА') or v.get('ВАРІЯНТ')))}</strong> · частота варіянта: <strong>{safe_int(v.get('ЧАСТОТА'), 0)}</strong></div>", unsafe_allow_html=True)
                        c1, c2, c3, c4 = st.columns([1.15, 1.15, 0.65, 0.45])
                        with c1:
                            vv = st.text_input("Варіянт", value=str(v.get("ВАРІЯНТ") or ""), key=f"edit_variant_v_{v['ID']}")
                            vs = st.text_input("Наголошена форма", value=str(v.get("НАГОЛОШЕНА ФОРМА") or ""), key=f"edit_variant_s_{v['ID']}")
                        with c2:
                            vt = st.text_input("Тип", value=str(v.get("ТИП") or ""), key=f"edit_variant_t_{v['ID']}")
                            vc = st.text_input("Коментар", value=str(v.get("КОМЕНТАР") or ""), key=f"edit_variant_c_{v['ID']}")
                        with c3:
                            vf = st.number_input("Частота", min_value=0, value=safe_int(v.get("ЧАСТОТА"), 0), step=1, key=f"edit_variant_f_{v['ID']}")
                            if safe_int(v.get("КІЛЬКІСТЬ_СЛОВОФОРМ"), 0):
                                st.caption("Є словоформи: частота обчислюється як їхня сума.")
                        with c4:
                            if st.button("💾", key=f"save_variant_{v['ID']}"):
                                lexdb.upsert_variant(int(v["ID"]), current_word_id, vv, vs, vf, vt, vc)
                                fast_rerun()
                            if st.button("🗑", key=f"delete_variant_{v['ID']}"):
                                lexdb.delete_variant(int(v["ID"]))
                                fast_rerun()

                        variant_wordforms = cached_lexdb_list_variant_wordforms(int(v["ID"]))
                        if variant_wordforms:
                            st.markdown("**Словоформи, зведені до цього варіянта:**")
                            for vwf in variant_wordforms:
                                ec1, ec2, ec3, ec4 = st.columns([1.05, 0.55, 1.25, 0.38])
                                with ec1:
                                    edit_vwf_value = st.text_input("Словоформа варіянта", value=str(vwf.get("СЛОВОФОРМА") or ""), key=f"edit_vwf_value_{vwf['ID']}")
                                    edit_vwf_styl = dictionary_option_multiselect("Стилістика", "СТИЛІСТИКА", str(vwf.get("СТИЛІСТИКА") or ""), key_prefix=f"edit_vwf_styl_{vwf['ID']}")
                                with ec2:
                                    edit_vwf_freq = st.number_input("Частота", min_value=0, value=safe_int(vwf.get("ЧАСТОТА"), 0), step=1, key=f"edit_vwf_freq_{vwf['ID']}")
                                with ec3:
                                    edit_vwf_grammar = grammar_multiselect_for_pos("Граматичні ознаки", pos, str(vwf.get("ГРАМАТИЧНІ ОЗНАКИ") or ""), key_prefix=f"edit_vwf_grammar_{vwf['ID']}_{pos}")
                                    edit_vwf_def = st.selectbox(
                                        "Значення",
                                        variant_def_options,
                                        index=variant_def_options.index(safe_int(vwf.get("ТЛУМАЧЕННЯ_ID"), 0)) if safe_int(vwf.get("ТЛУМАЧЕННЯ_ID"), 0) in variant_def_options else 0,
                                        format_func=lambda x: "— не вибрано —" if x == 0 else variant_def_labels.get(int(x), str(x)),
                                        key=f"edit_vwf_def_{vwf['ID']}",
                                    )
                                with ec4:
                                    if st.button("💾", key=f"save_vwf_{vwf['ID']}"):
                                        lexdb.upsert_variant_wordform(int(vwf["ID"]), int(v["ID"]), edit_vwf_value, edit_vwf_freq, edit_vwf_grammar, edit_vwf_styl, int(edit_vwf_def) if edit_vwf_def else None)
                                        st.success("Словоформу варіянта оновлено.")
                                        fast_rerun()
                                    if st.button("🗑", key=f"delete_vwf_{vwf['ID']}"):
                                        lexdb.delete_variant_wordform(int(vwf["ID"]))
                                        fast_rerun()

                        with st.expander(f"Додати словоформу до варіянта «{str(v.get('ВАРІЯНТ') or '')}»", expanded=False):
                            av1, av2, av3 = st.columns([1.05, 0.55, 1.35])
                            with av1:
                                add_vwf_value = st.text_input("Нова словоформа варіянта", placeholder="Напр.: катедрою, катедри", key=f"add_vwf_value_{v['ID']}")
                                add_vwf_styl = dictionary_option_multiselect("Стилістика словоформи", "СТИЛІСТИКА", "", key_prefix=f"add_vwf_styl_{v['ID']}")
                            with av2:
                                add_vwf_freq = st.number_input("Частота", min_value=0, value=0, step=1, key=f"add_vwf_freq_{v['ID']}")
                            with av3:
                                add_vwf_grammar = grammar_multiselect_for_pos("Граматичні ознаки словоформи", pos, "", key_prefix=f"add_vwf_grammar_{v['ID']}_{pos}")
                                add_vwf_def = st.selectbox(
                                    "Значення словоформи",
                                    variant_def_options,
                                    format_func=lambda x: "— не вибрано —" if x == 0 else variant_def_labels.get(int(x), str(x)),
                                    key=f"add_vwf_def_{v['ID']}",
                                )
                            if st.button("Додати словоформу варіянта й перерахувати частоту", key=f"add_variant_wordform_{v['ID']}"):
                                if add_vwf_value.strip():
                                    lexdb.upsert_variant_wordform(None, int(v["ID"]), add_vwf_value, add_vwf_freq, add_vwf_grammar, add_vwf_styl, int(add_vwf_def) if add_vwf_def else None)
                                    st.success("Словоформу варіянта додано. Частоту варіянта й загальну частоту леми перераховано.")
                                    fast_rerun()
                                else:
                                    st.error("Словоформа варіянта не може бути порожньою.")
                        st.markdown("---")
            st.markdown("---")
            ref1, ref2 = st.columns([1.4, 1])
            with ref1:
                ref_text = st.text_input("Відсилання →", placeholder="Напр.: державність; самостійність", key=f"dict_ref_text_{current_word_id}")
            with ref2:
                ref_type = st.selectbox("Тип відсилання", ["до основної статті", "до спорідненого слова", "до варіянта", "див. також"], key=f"dict_ref_type_{current_word_id}")
            target_word = word_id_select("Цільова стаття, якщо вже створена", all_words, key=f"dict_target_word_{current_word_id}")
            if st.button("Додати відсилання", key=f"dict_add_crossref_{current_word_id}"):
                if ref_text.strip():
                    lexdb.upsert_crossref(None, current_word_id, ref_type, ref_text, target_word)
                    st.success("Відсилання додано.")
                    fast_rerun()
            crossrefs = cached_lexdb_list_crossrefs(current_word_id)
            if crossrefs:
                with st.expander("Створені відсилання", expanded=False):
                    for r in crossrefs:
                        c1, c2, c3 = st.columns([1.6, 0.9, 0.35])
                        with c1:
                            rt = st.text_input("Текст", value=str(r.get("ТЕКСТ") or ""), key=f"edit_ref_text_{r['ID']}")
                        with c2:
                            rtype = st.text_input("Тип", value=str(r.get("ТИП") or ""), key=f"edit_ref_type_{r['ID']}")
                        with c3:
                            if st.button("💾", key=f"save_ref_{r['ID']}"):
                                lexdb.upsert_crossref(int(r["ID"]), current_word_id, rtype, rt, safe_int(r.get("ЦІЛЬОВЕ_СЛОВО_ID"), 0) or None)
                                fast_rerun()
                            if st.button("🗑", key=f"del_ref_{r['ID']}"):
                                lexdb.delete_crossref(int(r["ID"]))
                                fast_rerun()
            render_zone_box_end()

        dict_zone_header(5, "Словоформи й граматична довідка", "Формує зону •: засвідчені словоформи з частотністю, граматикою, стилістикою, значеннями та джерелами.")
        wf_box = keyed_container("dict_zone_wordforms")
        with wf_box:
            render_zone_box_start()
            definitions_for_wf = cached_lexdb_list_definitions(current_word_id)
            def_options_wf = [0] + [int(d["ID"]) for d in definitions_for_wf]
            def_labels_wf = {int(d["ID"]): f"{i + 1}. {str(d.get('ТЛУМАЧЕННЯ') or '')[:75]}" for i, d in enumerate(definitions_for_wf)}

            st.markdown(
                "<p class='dict-section-note'>Однакова словоформа може бути внесена кілька разів, якщо вона має инші граматичні ознаки, стилістичні ремарки або належить до иншого значення. Наприклад: <strong>офензива</strong> — <em>військ.</em> і <em>перен.</em>.</p>",
                unsafe_allow_html=True,
            )
            wf_existing = cached_lexdb_get_wordform(current_word_id, selected_wordform) if selected_wordform else None
            wf1, wf2, wf3 = st.columns([1.05, 0.6, 1.25])
            with wf1:
                wf_value = st.text_input("Словоформа", value=selected_wordform or "", key=f"dict_wf_value_{current_word_id}")
            with wf2:
                wf_frequency = st.number_input("Частота цієї реалізації", min_value=0, value=safe_int((wf_existing or {}).get("ЧАСТОТА"), selected_frequency), step=1, key=f"dict_wf_freq_{current_word_id}")
            with wf3:
                wf_definition_id = st.selectbox(
                    "Значення словоформи",
                    def_options_wf,
                    format_func=lambda x: "— не прив’язано до значення —" if x == 0 else def_labels_wf.get(int(x), str(x)),
                    key=f"dict_wf_def_{current_word_id}",
                )
            wf_grammar = grammar_multiselect_for_pos(
                "Граматичні ознаки цієї словоформи",
                pos,
                str((wf_existing or {}).get("ГРАМАТИЧНІ ОЗНАКИ") or ""),
                key_prefix=f"dict_wf_grammar_{current_word_id}_{pos}",
            )
            wf_stylistics = dictionary_option_multiselect(
                "Стилістичні ремарки цієї словоформи",
                "СТИЛІСТИКА",
                str((wf_existing or {}).get("СТИЛІСТИКА") or ""),
                key_prefix=f"dict_wf_styl_{current_word_id}",
            )
            if st.button("Додати окрему реалізацію словоформи", key=f"dict_save_wordform_{current_word_id}"):
                if wf_value.strip():
                    lexdb.upsert_wordform(
                        current_word_id,
                        wf_value.strip(),
                        int(wf_frequency),
                        pos=pos,
                        grammar=wf_grammar,
                        stylistics=wf_stylistics,
                        definition_id=int(wf_definition_id) if wf_definition_id else None,
                    )
                    lexdb.recompute_word_frequency(current_word_id)
                    st.success("Словоформу збережено як окрему реалізацію.")
                    fast_rerun()
                else:
                    st.error("Словоформа не може бути порожньою.")

            wordforms_existing = cached_lexdb_list_wordforms_for_word(current_word_id)
            if wordforms_existing:
                st.markdown(
                    "<div class='dict-badge-row'>" + "".join(
                        f"<span class='dict-badge'>• {html.escape(str(w.get('СЛОВОФОРМА')))} · {html.escape(str(w.get('ГРАМАТИЧНІ ОЗНАКИ') or '—'))} · {html.escape(str(w.get('СТИЛІСТИКА') or '—'))} · знач. {def_labels_wf.get(safe_int(w.get('ТЛУМАЧЕННЯ_ID'), 0), '—')} · {safe_int(w.get('ЧАСТОТА'),0)}</span>"
                        for w in wordforms_existing
                    ) + "</div>",
                    unsafe_allow_html=True,
                )
                with st.expander("Редагувати словоформи, граматику, стилістику й значення", expanded=False):
                    for wf in wordforms_existing:
                        st.markdown(f"<div class='dict-mini-card'><strong>{html.escape(str(wf.get('СЛОВОФОРМА') or ''))}</strong> · ID {safe_int(wf.get('ID'),0)}</div>", unsafe_allow_html=True)
                        c1, c2, c3 = st.columns([1.0, 0.55, 1.15])
                        with c1:
                            wfv = st.text_input("Словоформа", value=str(wf.get("СЛОВОФОРМА") or ""), key=f"edit_wf_value_{wf['ID']}")
                        with c2:
                            wff = st.number_input("Частота", min_value=0, value=safe_int(wf.get("ЧАСТОТА"), 0), step=1, key=f"edit_wf_freq_{wf['ID']}")
                        with c3:
                            wf_def_edit = st.selectbox(
                                "Значення",
                                def_options_wf,
                                index=def_options_wf.index(safe_int(wf.get("ТЛУМАЧЕННЯ_ID"), 0)) if safe_int(wf.get("ТЛУМАЧЕННЯ_ID"), 0) in def_options_wf else 0,
                                format_func=lambda x: "— не прив’язано —" if x == 0 else def_labels_wf.get(int(x), str(x)),
                                key=f"edit_wf_def_{wf['ID']}",
                            )
                        wfg = grammar_multiselect_for_pos(
                            "Граматичні ознаки",
                            pos,
                            str(wf.get("ГРАМАТИЧНІ ОЗНАКИ") or ""),
                            key_prefix=f"edit_wf_grammar_{wf['ID']}_{pos}",
                        )
                        wfs = dictionary_option_multiselect(
                            "Стилістичні ремарки",
                            "СТИЛІСТИКА",
                            str(wf.get("СТИЛІСТИКА") or ""),
                            key_prefix=f"edit_wf_styl_{wf['ID']}",
                        )
                        b1, b2 = st.columns([1, 1])
                        with b1:
                            if st.button("Зберегти цю словоформу", key=f"save_wf_{wf['ID']}"):
                                lexdb.update_wordform(
                                    int(wf["ID"]),
                                    wfv,
                                    wff,
                                    pos=str(wf.get("ЧАСТИНА МОВИ") or pos),
                                    grammar=wfg,
                                    stylistics=wfs,
                                    definition_id=int(wf_def_edit) if wf_def_edit else None,
                                )
                                st.success("Словоформу оновлено.")
                                fast_rerun()
                        with b2:
                            if st.button("Видалити цю словоформу", key=f"del_wf_{wf['ID']}"):
                                lexdb.delete_wordform(int(wf["ID"]))
                                fast_rerun()
            render_zone_box_end()

        dict_zone_header(6, "Семантична зона: значення й підзначення", "Заповнює таблиці ТЛУМАЧЕННЯ, ПОКЛИКАННЯ та ПІДЗНАЧЕННЯ.")
        sem_box = keyed_container("dict_zone_definitions")
        with sem_box:
            render_zone_box_start()
            definitions = cached_lexdb_list_definitions(current_word_id)
            if definitions:
                st.markdown("<p class='dict-section-note'>Редагуйте значення окремо. Підзначення будуть виведені після //.</p>", unsafe_allow_html=True)
                for i, d in enumerate(definitions, start=1):
                    with st.expander(f"Значення {i}: {str(d.get('ТЛУМАЧЕННЯ') or '')[:90]}", expanded=False):
                        dt = st.text_area("Текст тлумачення", value=str(d.get("ТЛУМАЧЕННЯ") or ""), height=90, key=f"edit_def_text_{d['ID']}")
                        dc1, dc2, dc3 = st.columns([0.75, 1.1, 0.8])
                        with dc1:
                            dfreq = st.number_input("Частота значення (автоматично за кількістю ілюстрацій)", min_value=0, value=safe_int(d.get("ЧАСТОТА"), 0), step=1, disabled=True, key=f"edit_def_freq_{d['ID']}")
                        with dc2:
                            dstyl = dictionary_option_multiselect("Стилістика значення", "СТИЛІСТИКА", str(d.get("СТИЛІСТИКА") or ""), key_prefix=f"edit_def_styl_{d['ID']}")
                        with dc3:
                            dref = reference_select("Покликання", "ПОКЛИКАННЯ", key=f"edit_def_ref_{d['ID']}", default_id=safe_int(d.get("ПОКЛИКАННЯ_ID"), 0) or None)
                        b1, b2 = st.columns([1, 1])
                        with b1:
                            if st.button("Зберегти значення", key=f"save_def_{d['ID']}"):
                                lexdb.update_definition(int(d["ID"]), dt, dref, dfreq, dstyl)
                                st.success("Значення оновлено.")
                                fast_rerun()
                        with b2:
                            if st.button("Видалити значення", key=f"delete_def_{d['ID']}"):
                                lexdb.delete_definition(int(d["ID"]))
                                fast_rerun()
                        subs = cached_lexdb_list_subdefinitions(int(d["ID"]))
                        if subs:
                            st.markdown("**Підзначення:**")
                            for sub in subs:
                                sc1, sc2 = st.columns([1.7, 0.3])
                                with sc1:
                                    st.markdown(f"<div class='dict-mini-card'>// {html.escape(str(sub.get('ТЕКСТ') or ''))} ({safe_int(sub.get('ЧАСТОТА'),0)}) {html.escape(str(sub.get('СТИЛІСТИКА') or ''))}</div>", unsafe_allow_html=True)
                                with sc2:
                                    if st.button("🗑", key=f"del_sub_{sub['ID']}"):
                                        lexdb.delete_subdefinition(int(sub["ID"]))
                                        fast_rerun()
                        sub_text = st.text_input("Нове підзначення //", key=f"new_sub_text_{d['ID']}")
                        sc1, sc2 = st.columns([0.6, 1.4])
                        with sc1:
                            sub_freq = st.number_input("Частота підзначення", min_value=0, value=0, step=1, key=f"new_sub_freq_{d['ID']}")
                        with sc2:
                            sub_styl = dictionary_option_multiselect("Стилістика підзначення", "СТИЛІСТИКА", "", key_prefix=f"new_sub_styl_{d['ID']}")
                        if st.button("Додати підзначення", key=f"add_sub_{d['ID']}"):
                            if sub_text.strip():
                                lexdb.upsert_subdefinition(None, int(d["ID"]), sub_text, sub_freq, sub_styl)
                                fast_rerun()
            st.markdown("#### Додати нове значення")
            new_def_text = st.text_area("Тлумачення / функційне пояснення", height=110, key=f"new_def_text_{current_word_id}")
            nd1, nd2, nd3 = st.columns([0.65, 1.15, 0.9])
            with nd1:
                new_def_freq = st.number_input("Частота значення (автоматично після прикріплення ілюстрацій)", min_value=0, value=0, step=1, disabled=True, key=f"new_def_freq_{current_word_id}")
            with nd2:
                new_def_styl = dictionary_option_multiselect("Стилістика значення", "СТИЛІСТИКА", "", key_prefix=f"new_def_styl_{current_word_id}")
            with nd3:
                new_def_ref = reference_select("Покликання", "ПОКЛИКАННЯ", key=f"new_def_ref_{current_word_id}")
            with st.expander("Додати / редагувати покликання для тлумачень", expanded=False):
                ref_abbr = st.text_input("Скорочення покликання", placeholder="Напр.: СУМ-11", key=f"new_ref_abbr_{current_word_id}")
                ref_full = st.text_input("Повна назва покликання", key=f"new_ref_full_{current_word_id}")
                if st.button("Зберегти покликання", key=f"save_new_ref_{current_word_id}"):
                    if ref_abbr.strip() or ref_full.strip():
                        lexdb.upsert_reference("ПОКЛИКАННЯ", ref_abbr, ref_full)
                        st.success("Покликання додано.")
                        fast_rerun()
                for ref_item in cached_lexdb_list_references("ПОКЛИКАННЯ")[:80]:
                    pc1, pc2, pc3 = st.columns([0.7, 1.7, 0.35])
                    with pc1:
                        pa = st.text_input("Скорочення", value=str(ref_item.get("СКОРОЧЕННЯ") or ""), key=f"edit_ref_abbr_full_{ref_item['ID']}")
                    with pc2:
                        pf = st.text_input("Повна назва", value=str(ref_item.get("ПОВНА НАЗВА") or ""), key=f"edit_ref_full_full_{ref_item['ID']}")
                    with pc3:
                        if st.button("💾", key=f"save_ref_full_{ref_item['ID']}"):
                            lexdb.update_reference("ПОКЛИКАННЯ", int(ref_item["ID"]), pa, pf)
                            fast_rerun()
            if st.button("Додати значення", key=f"add_def_{current_word_id}"):
                if new_def_text.strip():
                    lexdb.insert_definition(current_word_id, new_def_text, new_def_ref, new_def_freq, new_def_styl)
                    st.success("Значення додано.")
                    fast_rerun()
                else:
                    st.error("Тлумачення не може бути порожнім.")
            render_zone_box_end()

        dict_zone_header(7, "Ілюстративна й редакційна зона", "Добір, редагування й паспортизація цитат: ПЕРШОДРУК, ПЕРЕДРУК ПУР-1978, ДЖЕРЕЛО та зв’язки з таблицями.")
        quote_box = keyed_container("dict_zone_quotes")
        with quote_box:
            render_zone_box_start()
            definitions = cached_lexdb_list_definitions(current_word_id)
            collocations = cached_lexdb_list_collocations(current_word_id)
            wordforms_existing = cached_lexdb_list_wordforms_for_word(current_word_id)
            target_def_options = [0] + [int(d["ID"]) for d in definitions]
            target_def_labels = {int(d["ID"]): f"{i+1}. {str(d.get('ТЛУМАЧЕННЯ') or '')[:75]}" for i, d in enumerate(definitions)}
            target_coll_options = [0] + [int(c["ID"]) for c in collocations]
            target_coll_labels = {int(c["ID"]): str(c.get("ОДИНИЦЯ") or c.get("ID")) for c in collocations}
            target_wf_options = [0] + [int(w["ID"]) for w in wordforms_existing]
            target_wf_labels = {int(w["ID"]): f"{w.get('СЛОВОФОРМА')} · {w.get('ГРАМАТИЧНІ ОЗНАКИ') or '—'} · знач. {target_def_labels.get(safe_int(w.get('ТЛУМАЧЕННЯ_ID'),0), '—')} ({safe_int(w.get('ЧАСТОТА'),0)})" for w in wordforms_existing}

            st.markdown("#### Добір із конкордансу")
            qc1, qc2, qc3, qc4 = st.columns([1.2, 0.75, 1.0, 0.9])
            with qc1:
                dict_mode = st.selectbox("Режим контексту", ["Реченнєвий контекст", "Контекст фіксованої глибини"], index=0, key=f"dict_context_mode_full_{current_word_id}_{selected_wordform}")
            with qc2:
                if dict_mode == "Контекст фіксованої глибини":
                    dict_depth = st.number_input("Глибина", min_value=1, max_value=50, value=7, step=1, key=f"dict_context_depth_full_{current_word_id}_{selected_wordform}")
                else:
                    dict_depth = 7
            with qc3:
                dict_variants_choice = st.selectbox("Варіянти в конкордансі", ["Показувати", "Не показувати"], index=0, key=f"dict_context_variants_full_{current_word_id}_{selected_wordform}")
            with qc4:
                context_limit = st.number_input("Кількість контекстів", min_value=10, max_value=1000, value=80, step=10, key=f"dict_context_limit_full_{current_word_id}_{selected_wordform}")
            found_wordforms = get_wordforms_for_queries([selected_wordform], dict_variants_choice == "Показувати", wordforms_df) or [selected_wordform]
            available_codes_df = fetch_available_codes(STAMP, tuple(found_wordforms))
            code_options = available_codes_df["code"].tolist() if not available_codes_df.empty else []
            code_labels = {row.code: f"{row.code} — {row.title} ({int(row.hits)})" for row in available_codes_df.itertuples(index=False)}
            selected_codes = st.multiselect("Фільтр за кодом статті", options=code_options, default=[], format_func=lambda c: code_labels.get(c, c), key=f"dict_context_codes_full_{current_word_id}_{selected_wordform}")
            total_hits = count_contexts(STAMP, tuple(found_wordforms), tuple(selected_codes))
            header = format_register_header(found_wordforms, wordforms_df, dict_variants_choice == "Показувати")
            st.markdown(render_register_card(header, total_hits, len(available_codes_df), dict_mode, int(dict_depth)), unsafe_allow_html=True)
            ctx_df = fetch_contexts(STAMP, tuple(found_wordforms), dict_mode, int(dict_depth), tuple(selected_codes), int(context_limit))
            selected_contexts = pd.DataFrame() if ctx_df.empty else render_context_selector(ctx_df, selected_wordform)
            if ctx_df.empty:
                st.info("Для цієї словоформи немає контекстів.")

            edited_contexts: list[dict] = []
            if not selected_contexts.empty:
                with st.expander("Редагувати вибрані ілюстрації перед збереженням", expanded=True):
                    st.caption("Тут можна скоротити цитату через […], виправити межі контексту, указати PRG/SRG, вибрати джерело першодруку й додати передрук ПУР-1978.")
                    for local_i, (_, row) in enumerate(selected_contexts.iterrows(), start=1):
                        default_first = context_row_to_quote(row)
                        st.markdown(f"<div class='dict-mini-card'><strong>Ілюстрація {local_i}</strong> · {html.escape(str(row.get('code', '')))} · {html.escape(str(row.get('article_title', '')))}</div>", unsafe_allow_html=True)
                        ec1, ec2, ec3 = st.columns([0.45, 0.45, 1.25])
                        with ec1:
                            e_prg = st.number_input("PRG / позиція", min_value=0, value=safe_int(row.get("token_index"), 0), step=1, key=f"edit_ctx_prg_{current_word_id}_{local_i}_{safe_int(row.get('token_index'),0)}")
                        with ec2:
                            e_srg = st.number_input("SRG / сегмент", min_value=0, value=0, step=1, key=f"edit_ctx_srg_{current_word_id}_{local_i}_{safe_int(row.get('token_index'),0)}")
                        with ec3:
                            e_source = reference_select("Джерело першодруку", "ДЖЕРЕЛО", key=f"edit_ctx_source_{current_word_id}_{local_i}_{safe_int(row.get('token_index'),0)}")
                        e_first = st.text_area("Текст першодруку", value=default_first, height=88, key=f"edit_ctx_first_{current_word_id}_{local_i}_{safe_int(row.get('token_index'),0)}")
                        e_reprint = st.text_area("Передрук ПУР-1978 / редакційна відмінність ↕", value="", height=72, key=f"edit_ctx_reprint_{current_word_id}_{local_i}_{safe_int(row.get('token_index'),0)}")
                        edited_contexts.append({
                            "prg": int(e_prg) if e_prg else None,
                            "srg": int(e_srg) if e_srg else None,
                            "firstprint": e_first,
                            "reprint": e_reprint,
                            "source_id": e_source,
                            "code": str(row.get("code", "")),
                            "article_title": str(row.get("article_title", "")),
                        })

            at1, at2, at3 = st.columns([1.1, 1.1, 1.1])
            with at1:
                attach_wf_id = st.selectbox("Прикріпити до словоформи", target_wf_options, format_func=lambda x: "— не вибрано —" if x == 0 else target_wf_labels.get(int(x), str(x)), key=f"attach_wf_context_{current_word_id}")
            with at2:
                attach_def_id = st.selectbox("Прикріпити до значення", target_def_options, format_func=lambda x: "— не вибрано —" if x == 0 else target_def_labels.get(int(x), str(x)), key=f"attach_def_context_{current_word_id}")
            with at3:
                attach_coll_id = st.selectbox("Прикріпити до сталої сполуки", target_coll_options, format_func=lambda x: "— не вибрано —" if x == 0 else target_coll_labels.get(int(x), str(x)), key=f"attach_coll_context_{current_word_id}")
            if st.button("Зберегти вибрані / відредаговані контексти як цитати", key=f"save_context_quotes_{current_word_id}_{selected_wordform}"):
                if not edited_contexts:
                    st.error("Не вибрано жодного контексту.")
                else:
                    saved = 0
                    for item in edited_contexts:
                        source_id = item.get("source_id") or lexdb.upsert_reference("ДЖЕРЕЛО", str(item.get("code", "")), str(item.get("article_title", "")))
                        quote_id = lexdb.upsert_quote(prg=item.get("prg"), srg=item.get("srg"), firstprint=str(item.get("firstprint") or ""), reprint=str(item.get("reprint") or ""), source_id=source_id)
                        if attach_wf_id:
                            lexdb.link_wordform_quote(int(attach_wf_id), quote_id)
                        if attach_def_id:
                            lexdb.link_definition_quote(int(attach_def_id), quote_id)
                        if attach_coll_id:
                            lexdb.link_collocation_quote(int(attach_coll_id), quote_id)
                        saved += 1
                    st.success(f"Збережено цитат: {saved}.")
                    fast_rerun()

            st.markdown("---")
            st.markdown("#### Додати цитату вручну / редакційне зіставлення")
            mq1, mq2, mq3 = st.columns([0.55, 0.55, 1.3])
            with mq1:
                manual_prg = st.number_input("PRG / позиція", min_value=0, value=0, step=1, key=f"manual_prg_{current_word_id}")
            with mq2:
                manual_srg = st.number_input("SRG / сегмент", min_value=0, value=0, step=1, key=f"manual_srg_{current_word_id}")
            with mq3:
                manual_source_id = reference_select("Вибрати першодрук / джерело першодруку", "ДЖЕРЕЛО", key=f"manual_source_{current_word_id}")
            manual_first = st.text_area("Першодрук", height=90, key=f"manual_first_{current_word_id}")
            manual_reprint = st.text_area("Передрук ПУР-1978 / редакційна відмінність ↕", height=80, key=f"manual_reprint_{current_word_id}")
            ma1, ma2, ma3 = st.columns([1, 1, 1])
            with ma1:
                manual_wf_id = st.selectbox("Зв’язати зі словоформою", target_wf_options, format_func=lambda x: "— не вибрано —" if x == 0 else target_wf_labels.get(int(x), str(x)), key=f"manual_wf_{current_word_id}")
            with ma2:
                manual_def_id = st.selectbox("Зв’язати зі значенням", target_def_options, format_func=lambda x: "— не вибрано —" if x == 0 else target_def_labels.get(int(x), str(x)), key=f"manual_def_{current_word_id}")
            with ma3:
                manual_coll_id = st.selectbox("Зв’язати зі сполукою", target_coll_options, format_func=lambda x: "— не вибрано —" if x == 0 else target_coll_labels.get(int(x), str(x)), key=f"manual_coll_{current_word_id}")
            with st.expander("Додати / редагувати джерела першодруків", expanded=False):
                src_abbr = st.text_input("Скорочення джерела", key=f"new_source_abbr_{current_word_id}")
                src_full = st.text_input("Повна назва джерела", key=f"new_source_full_{current_word_id}")
                if st.button("Зберегти джерело", key=f"save_new_source_{current_word_id}"):
                    if src_abbr.strip() or src_full.strip():
                        lexdb.upsert_reference("ДЖЕРЕЛО", src_abbr, src_full)
                        st.success("Джерело додано.")
                        fast_rerun()
                for src in cached_lexdb_list_references("ДЖЕРЕЛО")[:80]:
                    sc1, sc2, sc3 = st.columns([0.7, 1.7, 0.35])
                    with sc1:
                        ea = st.text_input("Скорочення", value=str(src.get("СКОРОЧЕННЯ") or ""), key=f"edit_src_abbr_{src['ID']}")
                    with sc2:
                        ef = st.text_input("Повна назва", value=str(src.get("ПОВНА НАЗВА") or ""), key=f"edit_src_full_{src['ID']}")
                    with sc3:
                        if st.button("💾", key=f"save_src_{src['ID']}"):
                            lexdb.update_reference("ДЖЕРЕЛО", int(src["ID"]), ea, ef)
                            fast_rerun()
            if st.button("Додати цитату вручну", key=f"add_manual_quote_{current_word_id}"):
                if not manual_first.strip():
                    st.error("Текст першодруку не може бути порожнім.")
                else:
                    qid = lexdb.upsert_quote(manual_prg if manual_prg else None, manual_srg if manual_srg else None, manual_first, manual_reprint, manual_source_id)
                    if manual_wf_id:
                        lexdb.link_wordform_quote(int(manual_wf_id), qid)
                    if manual_def_id:
                        lexdb.link_definition_quote(int(manual_def_id), qid)
                    if manual_coll_id:
                        lexdb.link_collocation_quote(int(manual_coll_id), qid)
                    st.success("Цитату додано.")
                    fast_rerun()

            article_for_quotes = cached_lexdb_get_dictionary_article_full(current_word_id)
            existing_quotes = article_for_quotes.get("all_quotes") or []
            if existing_quotes:
                with st.expander("Редагувати вже збережені ілюстрації", expanded=False):
                    for q in existing_quotes:
                        with st.expander(f"Цитата ID {q.get('ID')} · {format_source_label(q)}", expanded=False):
                            attachments = cached_lexdb_get_quote_attachments(int(q["ID"]))
                            if attachments:
                                st.markdown("**Позиція цієї ілюстрації у статті:**")
                                kind_labels = {"definition": "значення", "wordform": "словоформи", "collocation": "сталої сполуки"}
                                for att in attachments:
                                    ac1, ac2, ac3, ac4 = st.columns([1.65, 0.42, 0.42, 0.9])
                                    with ac1:
                                        st.markdown(
                                            f"<div class='dict-mini-card'><strong>{html.escape(kind_labels.get(str(att.get('KIND')), str(att.get('KIND'))))}</strong>: "
                                            f"{html.escape(str(att.get('LABEL') or ''))} · позиція {safe_int(att.get('ПОРЯДОК'), 0) or '—'}</div>",
                                            unsafe_allow_html=True,
                                        )
                                    with ac2:
                                        if st.button("↑", key=f"quote_up_{att.get('KIND')}_{att.get('OWNER_ID')}_{q['ID']}"):
                                            lexdb.move_quote_link(str(att.get("KIND")), int(att.get("OWNER_ID")), int(q["ID"]), "up")
                                            fast_rerun()
                                    with ac3:
                                        if st.button("↓", key=f"quote_down_{att.get('KIND')}_{att.get('OWNER_ID')}_{q['ID']}"):
                                            lexdb.move_quote_link(str(att.get("KIND")), int(att.get("OWNER_ID")), int(q["ID"]), "down")
                                            fast_rerun()
                                    with ac4:
                                        if st.button("Упорядкувати", key=f"quote_norm_{att.get('KIND')}_{att.get('OWNER_ID')}_{q['ID']}"):
                                            lexdb.normalize_quote_order(str(att.get("KIND")), int(att.get("OWNER_ID")))
                                            fast_rerun()
                            else:
                                st.caption("Цитата ще не має активного прикріплення до словоформи, значення або сталої сполуки.")
                            eq1, eq2, eq3 = st.columns([0.45, 0.45, 1.2])
                            with eq1:
                                q_prg = st.number_input("PRG / позиція", min_value=0, value=safe_int(q.get("PRG"), 0), step=1, key=f"q_prg_{q['ID']}")
                            with eq2:
                                q_srg = st.number_input("SRG / сегмент", min_value=0, value=safe_int(q.get("SRG"), 0), step=1, key=f"q_srg_{q['ID']}")
                            with eq3:
                                q_source = reference_select("Джерело першодруку", "ДЖЕРЕЛО", key=f"q_source_{q['ID']}", default_id=safe_int(q.get("ДЖЕРЕЛО_ID"), 0) or None)
                            q_first = st.text_area("Першодрук", value=str(q.get("ПЕРШОДРУК") or ""), height=88, key=f"q_first_{q['ID']}")
                            q_reprint = st.text_area("Передрук ПУР-1978", value=str(q.get("ПЕРЕДРУК") or ""), height=72, key=f"q_reprint_{q['ID']}")
                            add1, add2, add3 = st.columns([1, 1, 1])
                            with add1:
                                q_add_wf = st.selectbox("Додатково прикріпити до словоформи", target_wf_options, format_func=lambda x: "— не вибрано —" if x == 0 else target_wf_labels.get(int(x), str(x)), key=f"q_add_wf_{q['ID']}")
                            with add2:
                                q_add_def = st.selectbox("Додатково прикріпити до значення", target_def_options, format_func=lambda x: "— не вибрано —" if x == 0 else target_def_labels.get(int(x), str(x)), key=f"q_add_def_{q['ID']}")
                            with add3:
                                q_add_coll = st.selectbox("Додатково прикріпити до сполуки", target_coll_options, format_func=lambda x: "— не вибрано —" if x == 0 else target_coll_labels.get(int(x), str(x)), key=f"q_add_coll_{q['ID']}")
                            qb1, qb2 = st.columns([1, 1])
                            with qb1:
                                if st.button("Зберегти цитату", key=f"save_quote_{q['ID']}"):
                                    lexdb.update_quote(int(q["ID"]), q_prg if q_prg else None, q_srg if q_srg else None, q_first, q_reprint, q_source)
                                    if q_add_wf:
                                        lexdb.link_wordform_quote(int(q_add_wf), int(q["ID"]))
                                    if q_add_def:
                                        lexdb.link_definition_quote(int(q_add_def), int(q["ID"]))
                                    if q_add_coll:
                                        lexdb.link_collocation_quote(int(q_add_coll), int(q["ID"]))
                                    st.success("Цитату оновлено.")
                                    fast_rerun()
                            with qb2:
                                if st.button("Видалити цитату", key=f"delete_quote_{q['ID']}"):
                                    lexdb.delete_quote(int(q["ID"]))
                                    fast_rerun()
            render_zone_box_end()

        dict_zone_header(8, "Фразеологічно-сполучувальна зона", "Формує зону ♦: сталі словосполуки, політичні формули, фразеологізми, термінологізовані сполуки.")
        coll_box = keyed_container("dict_zone_collocations")
        with coll_box:
            render_zone_box_start()
            collocations = cached_lexdb_list_collocations(current_word_id)
            if collocations:
                for c in collocations:
                    with st.expander(f"♦ {c.get('ОДИНИЦЯ')}", expanded=False):
                        cu = st.text_input("Одиниця", value=str(c.get("ОДИНИЦЯ") or ""), key=f"edit_coll_unit_{c['ID']}")
                        ct = dictionary_option_select("Тип одиниці", "ТИП СТІЙКОЇ СПОЛУКИ", str(c.get("ТИП") or ""), key_prefix=f"edit_coll_type_{c['ID']}")
                        cd = st.text_area("Тлумачення сполуки", value=str(c.get("ТЛУМАЧЕННЯ") or ""), height=80, key=f"edit_coll_def_{c['ID']}")
                        cc1, cc2 = st.columns([0.6, 1.4])
                        with cc1:
                            cf = st.number_input("Частота", min_value=0, value=safe_int(c.get("ЧАСТОТА"), 0), step=1, key=f"edit_coll_freq_{c['ID']}")
                        with cc2:
                            cs = dictionary_option_multiselect("Стилістика сполуки", "СТИЛІСТИКА", str(c.get("СТИЛІСТИКА") or ""), key_prefix=f"edit_coll_styl_{c['ID']}")
                        cb1, cb2 = st.columns([1, 1])
                        with cb1:
                            if st.button("Зберегти сполуку", key=f"save_coll_{c['ID']}"):
                                lexdb.update_collocation(int(c["ID"]), cu, ct, cd, cf, cs)
                                fast_rerun()
                        with cb2:
                            if st.button("Видалити сполуку", key=f"del_coll_{c['ID']}"):
                                lexdb.delete_collocation(int(c["ID"]))
                                fast_rerun()
            st.markdown("#### Додати нову сталу сполуку")
            unit = st.text_input("Одиниця", placeholder="Наприклад: визвольна боротьба", key=f"new_coll_unit_{current_word_id}")
            unit_type = dictionary_option_select("Тип одиниці", "ТИП СТІЙКОЇ СПОЛУКИ", "", key_prefix=f"new_coll_type_{current_word_id}")
            coll_definition = st.text_area("Тлумачення сполуки / фразеологізму", height=90, key=f"new_coll_definition_{current_word_id}")
            nc1, nc2 = st.columns([0.55, 1.45])
            with nc1:
                coll_frequency = st.number_input("Частота сполуки", min_value=0, value=0, step=1, key=f"new_coll_freq_{current_word_id}")
            with nc2:
                coll_stylistics = dictionary_option_multiselect("Стилістика сполуки", "СТИЛІСТИКА", "", key_prefix=f"new_coll_styl_{current_word_id}")
            if st.button("Додати сталу сполуку", key=f"add_collocation_{current_word_id}"):
                if unit.strip():
                    lexdb.insert_collocation(current_word_id, unit, unit_type, coll_definition, int(coll_frequency), coll_stylistics)
                    st.success("Сталу сполуку додано.")
                    fast_rerun()
                else:
                    st.error("Одиниця сталої сполуки не може бути порожньою.")
            render_zone_box_end()

        dict_zone_header(9, "Семантичні зв’язки", "Формує зони = та ≠: авторські синоніми, антоніми й споріднені одиниці.")
        rel_box = keyed_container("dict_zone_relations")
        with rel_box:
            render_zone_box_start()
            rel1, rel2 = st.columns([0.75, 1.4])
            with rel1:
                rel_type = st.selectbox("Тип зв’язку", ["синонім", "антонім", "споріднена одиниця"], key=f"new_rel_type_{current_word_id}")
            with rel2:
                rel_unit = st.text_input("Одиниця", placeholder="Напр.: самостійність", key=f"new_rel_unit_{current_word_id}")
            related_id = word_id_select("Пов’язана стаття, якщо вже створена", all_words, key=f"new_rel_related_{current_word_id}")
            rel_comment = st.text_input("Коментар до зв’язку", key=f"new_rel_comment_{current_word_id}")
            if st.button("Додати семантичний зв’язок", key=f"add_relation_{current_word_id}"):
                if rel_unit.strip():
                    lexdb.upsert_semantic_relation(None, current_word_id, rel_type, rel_unit, related_id, rel_comment)
                    st.success("Семантичний зв’язок додано.")
                    fast_rerun()
            relations = cached_lexdb_list_semantic_relations(current_word_id)
            if relations:
                st.markdown("<div class='dict-badge-row'>" + "".join(f"<span class='dict-badge'>{'=' if r.get('ТИП')=='синонім' else '≠' if r.get('ТИП')=='антонім' else '→'} {html.escape(str(r.get('ОДИНИЦЯ')))}</span>" for r in relations) + "</div>", unsafe_allow_html=True)
                with st.expander("Редагувати / вилучити семантичні зв’язки", expanded=False):
                    for r in relations:
                        rc1, rc2, rc3 = st.columns([0.7, 1.2, 0.35])
                        with rc1:
                            rt = st.selectbox("Тип", ["синонім", "антонім", "споріднена одиниця"], index=["синонім", "антонім", "споріднена одиниця"].index(str(r.get("ТИП") or "синонім")) if str(r.get("ТИП") or "") in ["синонім", "антонім", "споріднена одиниця"] else 0, key=f"edit_rel_type_{r['ID']}")
                        with rc2:
                            ru = st.text_input("Одиниця", value=str(r.get("ОДИНИЦЯ") or ""), key=f"edit_rel_unit_{r['ID']}")
                        with rc3:
                            if st.button("💾", key=f"save_rel_{r['ID']}"):
                                lexdb.upsert_semantic_relation(int(r["ID"]), current_word_id, rt, ru, safe_int(r.get("ПОВЯЗАНЕ_СЛОВО_ID"), 0) or None, str(r.get("КОМЕНТАР") or ""))
                                fast_rerun()
                            if st.button("🗑", key=f"del_rel_{r['ID']}"):
                                lexdb.delete_semantic_relation(int(r["ID"]))
                                fast_rerun()
            render_zone_box_end()

        article = cached_lexdb_get_dictionary_article_full(current_word_id)
        dict_zone_header(10, "Попередній перегляд і контроль повноти", "Автоматично формує словникову статтю з позначками /, →, ↕, ♦, •, =, ≠, ‹…› і дає змогу налаштувати її зовнішній вигляд.")
        prev_box = keyed_container("dict_zone_preview")
        with prev_box:
            render_zone_box_start()
            errors, warnings = validate_dictionary_article(article)
            if not errors and not warnings:
                st.success("Стаття має повну базову структуру.")
            else:
                if errors:
                    st.error("Обов’язкові елементи, які треба доповнити: " + "; ".join(errors))
                if warnings:
                    st.warning("Рекомендації для повнішого опису: " + "; ".join(warnings))

            st.markdown("#### Редактор стилю попереднього перегляду")
            st.caption("Ці налаштування змінюють лише вигляд попереднього перегляду й не змінюють лексикографічних даних у базі.")
            st1, st2, st3 = st.columns([1.2, 0.7, 0.7])
            with st1:
                preview_font_choice = st.selectbox(
                    "Шрифт статті",
                    [
                        "Georgia, Cambria, 'Times New Roman', serif",
                        "Cormorant Garamond, Georgia, serif",
                        "Inter, Segoe UI, Arial, sans-serif",
                        "Times New Roman, Times, serif",
                    ],
                    index=0,
                    key=f"preview_font_{current_word_id}",
                )
            with st2:
                preview_font_size = st.slider("Розмір", min_value=14, max_value=26, value=18, step=1, key=f"preview_font_size_{current_word_id}")
            with st3:
                preview_compact = st.checkbox("Компактніше", value=False, key=f"preview_compact_{current_word_id}")

            ready_entry_html = format_ready_dictionary_entry_html(article, preview_font_choice, int(preview_font_size), bool(preview_compact))
            ready_entry = format_ready_dictionary_entry(article)
            if ready_entry_html:
                st.markdown(ready_entry_html, unsafe_allow_html=True)
                st.markdown("<div class='dict-download-spacer'></div>", unsafe_allow_html=True)
                st.download_button(
                    "⬇️ Завантажити готову словникову статтю TXT",
                    data=ready_entry.encode("utf-8-sig"),
                    file_name=f"dictionary_article_{current_word_id}.txt",
                    mime="text/plain",
                    key=f"download_entry_{current_word_id}",
                )
            else:
                st.info("Після збереження реєстрової одиниці тут з’явиться попередній перегляд словникової статті.")
            render_zone_box_end()



# Зберігаємо повний редактор як окрему функцію, а вкладку «Словник»
# перетворюємо на дві підвкладки: перегляд і редагування.
render_dictionary_editor_tab = render_dictionary_tab


def render_dictionary_public_view() -> None:
    ensure_dictionary_db_ready()
    st.subheader("Словник мови Степана Бандери")
    st.caption("Публічний перегляд уже створених словникових статей без доступу до редагування.")
    query = st.text_input("Пошук словникової статті", placeholder="Наприклад: боротьба", key="dict_view_search")
    words = cached_lexdb_search_words(query, limit=800)
    if not words:
        st.info("Поки що за цим запитом словникових статей не знайдено.")
        return
    selected_id = st.selectbox(
        "Вибрати статтю для перегляду",
        options=[int(w["ID"]) for w in words],
        format_func=lambda x: next((str(w.get("РЕЄСТРОВА ОДИНИЦЯ")) for w in words if int(w["ID"]) == int(x)), str(x)),
        key="dict_view_article_select",
    )
    article = cached_lexdb_get_dictionary_article_full(int(selected_id))

    st.markdown("#### Налаштування вигляду статті")
    st.caption("Ці налаштування змінюють лише вигляд публічного перегляду й не змінюють даних у базі.")
    vc1, vc2, vc3 = st.columns([1.2, 0.7, 0.7])
    with vc1:
        view_font_choice = st.selectbox(
            "Шрифт статті",
            [
                "Georgia, Cambria, 'Times New Roman', serif",
                "Cormorant Garamond, Georgia, serif",
                "Inter, Segoe UI, Arial, sans-serif",
                "Times New Roman, Times, serif",
            ],
            index=0,
            key=f"dict_view_font_{selected_id}",
        )
    with vc2:
        view_font_size = st.slider("Розмір", min_value=14, max_value=26, value=18, step=1, key=f"dict_view_font_size_{selected_id}")
    with vc3:
        view_compact = st.checkbox("Компактніше", value=False, key=f"dict_view_compact_{selected_id}")

    ready_entry_html = format_ready_dictionary_entry_html(article, view_font_choice, int(view_font_size), bool(view_compact))
    ready_entry = format_ready_dictionary_entry(article)
    if ready_entry_html:
        st.markdown(ready_entry_html, unsafe_allow_html=True)
        st.markdown("<div class='dict-download-spacer'></div>", unsafe_allow_html=True)
        st.download_button(
            "⬇️ Завантажити словникову статтю TXT",
            data=ready_entry.encode("utf-8-sig"),
            file_name=f"dictionary_article_{selected_id}.txt",
            mime="text/plain",
            key=f"dict_view_download_{selected_id}",
        )
    else:
        st.info("Статтю ще не заповнено достатньо для попереднього перегляду.")


def render_dictionary_tab(wordforms_df: pd.DataFrame) -> None:  # type: ignore[no-redef]
    render_dictionary_css()
    with keyed_container("dict_lower_tabs"):
        view_tab, edit_tab = st.tabs(["👁︎ Дивитись", "✍︎ Редагувати"])
        with view_tab:
            render_dictionary_public_view()
        with edit_tab:
            render_dictionary_editor_tab(wordforms_df)


st.markdown(
    """
    <style>
    @import url("https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,700;1,600;1,700&family=Inter:wght@400;500;600;700;800;900&display=swap");

    :root {
        --brand-burgundy: #76001f;
        --brand-burgundy-dark: #3a000d;
        --brand-burgundy-deep: #0d0003;
        --brand-wine: #5b0018;
        --brand-gold: #f1c65a;
        --brand-gold-light: #fff4b1;
        --brand-gold-deep: #9a6420;
        --paper: #fffaf1;
        --paper-soft: #fff5e8;
        --paper-line: rgba(167, 105, 39, 0.38);
        --text-main: #FFF8ECF5;
        --muted: #6f5962;
    }

    [data-testid="stHeader"] { display: none !important; height: 0 !important; }
    [data-testid="stToolbar"], #MainMenu, footer { visibility: hidden !important; height: 0 !important; }

    html, body, [class*="css"], [data-testid="stAppViewContainer"] {
        font-family: "Inter", "Segoe UI", "Roboto", "Arial", sans-serif;
        color: var(--text-main);
    }

    [data-testid="stAppViewContainer"] {
        background:
            radial-gradient(circle at 0% 0%, rgba(4,0,2,0.99), transparent 26rem),
            radial-gradient(circle at 100% 0%, rgba(5,0,2,0.99), transparent 28rem),
            radial-gradient(circle at 80% 8%, rgba(255,211,113,0.18), transparent 17rem),
            radial-gradient(circle at 18% 22%, rgba(95,0,31,0.50), transparent 30rem),
            radial-gradient(circle at 60% 64%, rgba(152,0,44,0.25), transparent 36rem),
            radial-gradient(circle at 44% 36%, rgba(255,110,112,0.08), transparent 24rem),
            linear-gradient(118deg, transparent 0%, transparent 3.8%, rgba(255, 222, 139, 0.17) 5.0%, rgba(255, 145, 86, 0.065) 7.3%, transparent 11.3%, transparent 100%),
            linear-gradient(128deg, transparent 0%, transparent 30%, rgba(255, 216, 128, 0.060) 31.5%, rgba(255, 84, 108, 0.075) 34%, transparent 38%, transparent 100%),
            linear-gradient(116deg, transparent 0%, transparent 42%, rgba(255, 70, 98, 0.14) 47.3%, rgba(255, 184, 95, 0.10) 49.0%, transparent 53.8%, transparent 100%),
            linear-gradient(145deg,
                #070002 0%,
                #150005 12%,
                #2c000b 29%,
                #4c0014 47%,
                #76001f 61%,
                #600018 76%,
                #2d000a 100%);
        background-attachment: fixed;
    }
    [data-testid="stAppViewContainer"]::before {
        content: "";
        position: fixed;
        inset: 0;
        pointer-events: none;
        background:
            linear-gradient(118deg, transparent 0%, transparent 8%, rgba(255, 229, 159, 0.12) 10.2%, rgba(255, 165, 91, 0.045) 12.4%, transparent 16%, transparent 100%),
            linear-gradient(130deg, transparent 0%, transparent 56%, rgba(255, 221, 139, 0.075) 58%, rgba(255, 91, 115, 0.06) 60.2%, transparent 64%, transparent 100%),
            radial-gradient(circle at 88% 8%, rgba(255,220,128,0.15), transparent 17rem),
            radial-gradient(circle at 8% 90%, rgba(0,0,0,0.30), transparent 28rem);
        opacity: 0.96;
        z-index: 0;
    }
    [data-testid="stAppViewContainer"]::after {
        content: "";
        position: fixed;
        inset: 0;
        pointer-events: none;
        background:
            linear-gradient(105deg, transparent 0%, transparent 68%, rgba(255, 210, 126, 0.055) 70%, rgba(255, 58, 94, 0.045) 72%, transparent 76%, transparent 100%),
            radial-gradient(ellipse at 74% 24%, rgba(255, 197, 113, 0.070), transparent 24rem);
        opacity: 0.8;
        z-index: 0;
    }

    .block-container {
        padding-top: 1.05rem;
        padding-bottom: 1.35rem;
        max-width: 1400px;
        position: relative;
        z-index: 1;
    }

    /* Шапка */
    .hero-panel {
        display: grid;
        grid-template-columns: minmax(0, 1fr) clamp(312px, 24.5vw, 400px);
        align-items: flex-start;
        gap: clamp(2.25rem, 4.25vw, 5.25rem);
        margin: 0 0 1.05rem 0;
        padding: 0.65rem 0.2rem 0.72rem 0.2rem;
    }
    .hero-text { min-width: 0; padding-top: 0.28rem; }
    .site-brand {
        display: inline-block;
        font-family: "Cormorant Garamond", "Georgia", serif;
        font-size: clamp(74px, 6.25vw, 106px);
        font-weight: 700;
        letter-spacing: 0.026em;
        text-align: left;
        margin: 0 0 0.34rem 0;
        padding: 0;
        line-height: 0.90;
        white-space: nowrap;
        overflow: visible;
        color: #f6d062;
        background: linear-gradient(180deg,
            #fff8bb 0%,
            #ffe478 16%,
            #f6c64d 34%,
            #b97a27 55%,
            #e6b743 73%,
            #fff2a4 100%);
        -webkit-background-clip: text;
        background-clip: text;
        -webkit-text-fill-color: transparent;
        text-shadow:
            0 1px 0 rgba(255,255,225,0.32),
            0 2px 0 rgba(94,51,13,0.28),
            0 4px 4px rgba(46,0,9,0.35),
            0 11px 18px rgba(0,0,0,0.50),
            0 24px 36px rgba(0,0,0,0.37);
        filter: none;
        -webkit-font-smoothing: antialiased;
        text-rendering: geometricPrecision;
    }
    .site-subtitle {
        font-size: clamp(18px, 1.45vw, 22px);
        line-height: 1.22;
        font-weight: 780;
        color: rgba(255, 246, 229, 0.92);
        letter-spacing: 0.022em;
        margin: 0.25rem 0 1.28rem 0;
        text-shadow: 0 3px 15px rgba(0,0,0,0.46);
    }
    .hero-quote {
        position: relative;
        display: inline-block;
        max-width: 760px;
        margin: 0;
        padding-left: 4.35rem;
        font-family: "Cormorant Garamond", "Georgia", serif;
        font-style: italic;
        font-size: clamp(24px, 1.95vw, 34px);
        font-weight: 700;
        line-height: 1.24;
        color: #f3d68e;
        text-shadow: 0 3px 16px rgba(0,0,0,0.52);
    }
    .hero-quote::before {
        content: "“";
        position: absolute;
        left: 1.1rem;
        top: -1.28rem;
        font-family: "Cormorant Garamond", "Georgia", serif;
        font-size: 88px;
        line-height: 1;
        color: rgba(241,198,90,0.58);
    }
    .hero-quote::after {
        content: "";
        position: absolute;
        left: 0;
        top: 0.12rem;
        width: 2px;
        height: calc(100% - 0.1rem);
        background: linear-gradient(180deg, transparent, rgba(241,198,90,0.60), transparent);
    }
    .hero-portrait-card {
        flex: 0 0 auto;
        width: clamp(312px, 24.5vw, 400px);
        height: clamp(325px, 25.5vw, 415px);
        border-radius: 24px;
        overflow: hidden;
        border: 5px solid rgba(242, 205, 96, 0.96);
        box-shadow:
            0 26px 66px rgba(0,0,0,0.45),
            0 0 0 2px rgba(115,65,18,0.64) inset,
            0 0 0 1px rgba(255,248,214,0.60),
            0 0 50px rgba(241,198,90,0.30),
            -18px 8px 48px rgba(135,0,42,0.24);
        background: #120004;
        position: relative;
    }
    .hero-portrait-card::before {
        content: "";
        position: absolute;
        inset: -18px;
        background: radial-gradient(circle at 50% 45%, rgba(255,213,118,0.18), transparent 62%);
        z-index: -1;
    }
    .hero-portrait-card img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        object-position: center 18%;
        display: block;
    }

    @keyframes tabLightSweep {
        0% { transform: translateX(-140%) skewX(-18deg); opacity: 0; }
        22% { opacity: 0.34; }
        100% { transform: translateX(170%) skewX(-18deg); opacity: 0; }
    }
    @keyframes activeTabSettle {
        0% { transform: translateY(0) scaleX(1); }
        45% { transform: translateY(-3px) scaleX(1.04); }
        100% { transform: translateY(-1px) scaleX(1.015); }
    }

    /* Вкладки */
    div[data-baseweb="tab-list"] {
        gap: 0.34rem;
        border-bottom: 2px solid rgba(235, 198, 111, 0.72);
        margin-top: 0.15rem;
        position: sticky;
        top: 0;
        z-index: 60;
        padding: 0 0 0.14rem 0;
        background: transparent;
        box-shadow: 0 14px 28px rgba(0,0,0,0.11);
        backdrop-filter: none;
        overflow: visible;
    }
    button[role="tab"] {
        padding: 0.98rem 1.9rem 0.92rem 1.9rem !important;
        border-radius: 15px 15px 0 0 !important;
        font-size: 18px !important;
        font-weight: 820 !important;
        color: #5a3b25 !important;
        background: linear-gradient(180deg, #fffdf8 0%, #f7eadf 100%) !important;
        border: 1px solid rgba(135,84,37,0.34) !important;
        border-bottom: none !important;
        box-shadow:
            0 -2px 9px rgba(255,255,255,0.72) inset,
            0 10px 18px rgba(0,0,0,0.10);
        transition: transform 0.26s cubic-bezier(.2,.8,.2,1), box-shadow 0.26s ease, color 0.22s ease, background 0.22s ease, padding 0.26s ease;
        margin-right: 0 !important;
        position: relative;
        overflow: hidden;
        isolation: isolate;
        min-width: 0;
    }
    button[role="tab"]::before {
        content: "";
        position: absolute;
        top: -18%;
        left: 0;
        width: 45%;
        height: 138%;
        background: linear-gradient(90deg, rgba(255,255,255,0), rgba(255,255,255,0.34), rgba(255,247,220,0));
        transform: translateX(-145%) skewX(-18deg);
        pointer-events: none;
        z-index: 2;
    }
    button[role="tab"]::after {
        content: "";
        position: absolute;
        inset: 0;
        background: linear-gradient(180deg, rgba(255,255,255,0.50), rgba(255,255,255,0.02) 44%, rgba(130,20,44,0.05));
        pointer-events: none;
        z-index: 1;
        border-radius: 15px 15px 0 0;
    }
    button[role="tab"]:hover {
        color: var(--brand-burgundy) !important;
        background: linear-gradient(180deg, #ffffff 0%, #fbf1e6 100%) !important;
        transform: translateY(-3px) scaleX(1.018);
        box-shadow:
            0 -2px 9px rgba(255,255,255,0.78) inset,
            0 15px 24px rgba(55,0,13,0.18);
    }
    button[role="tab"]:hover::before { animation: tabLightSweep 0.9s ease forwards; }
    button[role="tab"][aria-selected="true"] {
        color: #fff7df !important;
        background: linear-gradient(180deg, #a80f38 0%, #78051f 52%, #560014 100%) !important;
        border-color: rgba(245,213,116,0.76) !important;
        padding-left: 2.15rem !important;
        padding-right: 2.15rem !important;
        transform: translateY(-1px) scaleX(1.018);
        box-shadow:
            0 -1px 8px rgba(255,255,255,0.20) inset,
            0 15px 26px rgba(48,0,11,0.24),
            0 0 0 1px rgba(245,213,116,0.18) inset;
        animation: activeTabSettle 0.34s cubic-bezier(.2,.8,.2,1);
    }
    button[role="tab"][aria-selected="true"]::before { animation: tabLightSweep 1.05s ease forwards; }
    button[role="tab"] p {
        display: inline-flex !important;
        align-items: center !important;
        gap: 0.58rem !important;
        position: relative;
        z-index: 3;
    }
    button[role="tab"] p::before {
        content: "";
        width: 21px;
        height: 21px;
        display: inline-block;
        background-color: currentColor;
        opacity: 0.92;
        flex: 0 0 auto;
        -webkit-mask: var(--tab-icon) center / contain no-repeat;
        mask: var(--tab-icon) center / contain no-repeat;
    }
    button[role="tab"]:nth-of-type(1) p { --tab-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round'%3E%3Ccircle cx='10' cy='10' r='6.5'/%3E%3Cpath d='M15.2 15.2 21 21'/%3E%3C/svg%3E"); }
    button[role="tab"]:nth-of-type(2) p { --tab-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.1' stroke-linecap='round'%3E%3Cpath d='M5 20V10M12 20V4M19 20v-7'/%3E%3C/svg%3E"); }
    button[role="tab"]:nth-of-type(3) p { --tab-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.0' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 6.5c2.6-1.2 5.3-1.2 8 0v13c-2.7-1.2-5.4-1.2-8 0zM12 6.5c2.7-1.2 5.4-1.2 8 0v13c-2.6-1.2-5.3-1.2-8 0z'/%3E%3C/svg%3E"); }
    button[role="tab"]:nth-of-type(4) p { --tab-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.0' stroke-linecap='round'%3E%3Cpath d='M8 6h12M8 12h12M8 18h12'/%3E%3Cpath d='M4 6h.01M4 12h.01M4 18h.01'/%3E%3C/svg%3E"); }
    button[role="tab"]:nth-of-type(5) p { --tab-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.0' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M5 4.8c2.7-1 5.1-.8 7 .6v14.2c-1.9-1.4-4.3-1.6-7-.6z'/%3E%3Cpath d='M12 5.4c1.9-1.4 4.3-1.6 7-.6V19c-2.7-1-5.1-.8-7 .6z'/%3E%3Cpath d='M8 8h1.8M8 11h1.8M14.3 8H16M14.3 11H16'/%3E%3C/svg%3E"); }
    button[role="tab"]:nth-of-type(6) p { --tab-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.0' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M7 7h10M17 7l-3-3M17 7l-3 3M17 17H7M7 17l3-3M7 17l3 3'/%3E%3C/svg%3E"); }
    button[role="tab"]:nth-of-type(7) p { --tab-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.0' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M5 4.8c2.1-.9 4.3-.9 6.5 0V20c-2.2-.9-4.4-.9-6.5 0zM12.5 4.8c2.2-.9 4.4-.9 6.5 0V20c-2.1-.9-4.3-.9-6.5 0z'/%3E%3Cpath d='M12 5v15'/%3E%3C/svg%3E"); }

    /* Головна пошукова зона */
    [data-testid="stForm"] {
        background: linear-gradient(180deg, rgba(255,254,250,0.998), rgba(255,249,240,0.994));
        border: 2px solid rgba(176, 112, 44, 0.58);
        border-radius: 22px;
        padding: 1.65rem 1.8rem 1.55rem 1.8rem;
        box-shadow:
            0 26px 62px rgba(22,0,7,0.24),
            0 0 0 1px rgba(255,255,255,0.86) inset,
            0 0 0 4px rgba(120,5,31,0.20);
        margin-top: 1.15rem;
    }
    [data-testid="stForm"] label p {
        font-size: 18.5px;
        font-weight: 850;
        color: #2a1e25;
        margin-bottom: 0.35rem;
    }
    textarea, input {
        border-radius: 10px !important;
        font-size: 19px !important;
        accent-color: var(--brand-burgundy) !important;
        color: #171016 !important;
    }
    textarea {
        border: 1px solid rgba(176,112,44,0.34) !important;
        background: rgba(255,255,255,0.985) !important;
        box-shadow: 0 2px 8px rgba(0,0,0,0.05) inset;
    }
    div[data-testid="InputInstructions"] { display: none !important; }
    div[data-testid="stFormSubmitButton"] {
        display: flex;
        align-items: flex-start;
        justify-content: center;
        margin-top: 0.10rem;
    }
    div[data-testid="stFormSubmitButton"] button,
    div.stButton > button[kind="primary"],
    div.stButton > button {
        min-height: 60px !important;
        padding: 0.6rem 1.85rem !important;
        background: linear-gradient(180deg, #910a30 0%, #650019 100%) !important;
        color: #FFFFFF !important;
        border: 2px solid rgba(214, 172, 84, 0.78) !important;
        border-radius: 11px !important;
        font-size: 19px !important;
        font-weight: 850 !important;
        box-shadow: 0 13px 24px rgba(96,0,23,0.28), 0 0 0 1px rgba(255,255,255,0.12) inset;
    }
    div[data-testid="stFormSubmitButton"] button:hover,
    div.stButton > button:hover {
        background: linear-gradient(180deg, #a9133b 0%, #72001d 100%) !important;
        border-color: rgba(239,197,112,0.88) !important;
        color: #FFFFFF !important;
        transform: translateY(-1px);
    }

    /* Розширені налаштування */
    [data-testid="stExpander"] {
        border: 1px solid rgba(176,112,44,0.32) !important;
        border-radius: 16px !important;
        background: rgba(255,253,247,0.99) !important;
        box-shadow: 0 1px 0 rgba(255,255,255,0.92) inset;
        overflow: hidden;
    }
    [data-testid="stExpander"] summary {
        background: rgba(255,250,244,0.995) !important;
        border-bottom: 1px solid rgba(176,112,44,0.24) !important;
        color: var(--brand-burgundy) !important;
        font-size: 18px !important;
        font-weight: 820 !important;
    }
    [data-testid="stExpander"] label p,
    [data-testid="stExpander"] p {
        font-size: 16.5px;
        color: #24171f !important;
    }
    [data-testid="stForm"] [data-testid="stExpander"] label p,
    [data-testid="stForm"] [data-testid="stExpander"] p,
    [data-testid="stForm"] [data-testid="stRadio"] [role="radiogroup"] *,
    [data-testid="stForm"] [data-testid="stCheckbox"] *,
    [data-testid="stForm"] [data-testid="stNumberInput"] label p,
    [data-testid="stForm"] [data-testid="stTextArea"] label p,
    [data-testid="stForm"] [data-testid="stSelectbox"] label p {
        color: #24171f !important;
    }
    .advanced-note {
        color: #66545c;
        font-size: 15px;
        line-height: 1.45;
        margin-top: -0.2rem;
    }

    [data-baseweb="checkbox"] [aria-checked="true"],
    [data-baseweb="checkbox"] [data-checked="true"],
    [data-baseweb="radio"] [aria-checked="true"],
    [data-baseweb="radio"] [data-checked="true"],
    [role="checkbox"][aria-checked="true"],
    [role="radio"][aria-checked="true"] {
        background-color: var(--brand-burgundy) !important;
        border-color: var(--brand-burgundy) !important;
    }

    .stMarkdown, .stCaption, .stTextInput, .stTextArea, .stNumberInput, .stCheckbox, .stRadio, .stSelectbox, .stMultiSelect {
        font-family: "Inter", "Segoe UI", "Roboto", "Arial", sans-serif !important;
    }
    h1, h2, h3, h4 { font-family: "Inter", "Segoe UI", "Roboto", "Arial", sans-serif !important; letter-spacing: -0.01em; }
    div[data-testid="stHeadingWithActionElements"] h1,
    div[data-testid="stHeadingWithActionElements"] h2,
    div[data-testid="stHeadingWithActionElements"] h3,
    div[data-testid="stHeadingWithActionElements"] p,
    [data-testid="stMetricLabel"] div,
    [data-testid="stMetricValue"] div,
    div[data-testid="stRadio"] > label p,
    div[data-testid="stRadio"] [role="radiogroup"] *,
    div[data-testid="stTextInput"] > label p,
    div[data-testid="stNumberInput"] > label p,
    div[data-testid="stSelectbox"] > label p,
    div[data-testid="stMultiSelect"] > label p {
        color: rgba(255, 248, 236, 0.96) !important;
    }
    [data-testid="stForm"] div[data-testid="stRadio"] [role="radiogroup"] *,
    [data-testid="stForm"] div[data-testid="stTextInput"] > label p,
    [data-testid="stForm"] div[data-testid="stNumberInput"] > label p,
    [data-testid="stForm"] div[data-testid="stSelectbox"] > label p,
    [data-testid="stForm"] div[data-testid="stMultiSelect"] > label p,
    [data-testid="stForm"] div[data-testid="stCheckbox"] * {
        color: #24171f !important;
    }
    [data-baseweb="select"] div[role="button"] { background: rgba(255,255,255,0.96) !important; }

    /* Видимість службових написів і прапорців на темному тлі поза світлими панелями */
    div[data-testid="stCheckbox"] label p,
    div[data-testid="stCheckbox"] label span,
    div[data-testid="stCheckbox"] p,
    div[data-testid="stCheckbox"] span,
    div[data-testid="stRadio"] label p,
    div[data-testid="stRadio"] label span {
        color: rgba(255, 248, 236, 0.94) !important;
    }
    [data-testid="stForm"] div[data-testid="stCheckbox"] label p,
    [data-testid="stForm"] div[data-testid="stCheckbox"] label span,
    [data-testid="stForm"] div[data-testid="stCheckbox"] p,
    [data-testid="stForm"] div[data-testid="stCheckbox"] span,
    [data-testid="stForm"] div[data-testid="stRadio"] label p,
    [data-testid="stForm"] div[data-testid="stRadio"] label span {
        color: #24171f !important;
    }


    /* Кнопки завантаження мають чорний / темний текст на світлій кнопці */
    div[data-testid="stDownloadButton"] button,
    div[data-testid="stDownloadButton"] button *,
    div[data-testid="stDownloadButton"] button p,
    div[data-testid="stDownloadButton"] button span {
        color: #24171f !important;
    }
    div[data-testid="stDownloadButton"] button {
        background: rgba(255,255,255,0.96) !important;
        border: 1px solid rgba(176,112,44,0.34) !important;
        box-shadow: 0 6px 14px rgba(0,0,0,0.08) !important;
    }

    /* Повідомлення після конкордансу: світлий текст на темному фоні */
    .limit-info-note {
        margin: 1rem 0 1.05rem 0;
        padding: 0.95rem 1.1rem;
        border-radius: 14px;
        background: rgba(255, 246, 229, 0.10);
        border: 1px solid rgba(241, 198, 90, 0.28);
        color: rgba(255, 248, 236, 0.96) !important;
        font-size: 17px;
        font-weight: 620;
        box-shadow: 0 8px 22px rgba(0,0,0,0.10);
    }
    .limit-info-note * { color: rgba(255, 248, 236, 0.96) !important; }
    .concordance-download-spacer {
        height: 1.05rem;
        margin-top: 0.75rem;
        border-top: 1px solid rgba(241,198,90,0.20);
    }

    /* Тексти чекбоксів для підготовки повних файлів / ZIP на темному фоні */
    div[data-testid="stCheckbox"] label,
    div[data-testid="stCheckbox"] label *,
    div[data-testid="stCheckbox"] p,
    div[data-testid="stCheckbox"] span {
        color: rgba(255, 248, 236, 0.96) !important;
    }
    [data-testid="stForm"] div[data-testid="stCheckbox"] label,
    [data-testid="stForm"] div[data-testid="stCheckbox"] label *,
    [data-testid="stForm"] div[data-testid="stCheckbox"] p,
    [data-testid="stForm"] div[data-testid="stCheckbox"] span {
        color: #24171f !important;
    }

    /* Результати конкордансу */
    .random-wordforms-title {
        margin: 0.62rem auto 0.32rem auto;
        text-align: center;
        color: #f6d062 !important;
        font-weight: 950;
        letter-spacing: 0.02em;
        text-shadow: 0 0 12px rgba(246,208,98,0.28);
    }

    /* Компактний центрований ряд випадкових словоформ */
    div[class*="st-key-random_wordforms_row"] {
        width: min(100%, 980px) !important;
        margin: 0.1rem auto 0.8rem auto !important;
        display: flex !important;
        flex-wrap: wrap !important;
        justify-content: center !important;
        align-items: center !important;
        gap: 9px 14px !important;
    }
    div[class*="st-key-random_wordforms_row"] > div {
        width: auto !important;
        min-width: 0 !important;
        max-width: none !important;
        flex: 0 0 auto !important;
    }
    div[class*="st-key-random_wordforms_row"] div[data-testid="stButton"] {
        width: auto !important;
        margin: 0 !important;
    }
    div[class*="st-key-random_wordforms_row"] button {
        width: auto !important;
        min-width: 0 !important;
        max-width: 190px !important;
        min-height: 33px !important;
        padding: 0.15rem 0.55rem !important;
        color: #f6d062 !important;
        font-weight: 950 !important;
        font-size: clamp(12.5px, 0.95vw, 14px) !important;
        border: 1px solid rgba(246,208,98,0.38) !important;
        border-radius: 10px !important;
        background: rgba(84, 0, 22, 0.30) !important;
        box-shadow: 0 0 10px rgba(246,208,98,0.10) !important;
        transition: box-shadow 0.18s ease, transform 0.18s ease, color 0.18s ease !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
    }
    div[class*="st-key-random_wordforms_row"] button:hover {
        color: #fff4b1 !important;
        box-shadow: 0 0 18px rgba(246,208,98,0.62), 0 0 34px rgba(246,208,98,0.28) !important;
        transform: translateY(-1px);
    }

    .register-card {
        background: rgba(255,250,247,0.88);
        border: 1px solid rgba(176,112,44,0.18);
        border-radius: 18px;
        padding: 1rem 1.2rem;
        margin: 1.2rem 0 1rem 0;
        box-shadow: 0 10px 30px rgba(72,0,18,0.08);
    }
    .headword { width: 100%; text-align: center; font-size: 34px; font-weight: 950; letter-spacing: 0.02em; color: var(--brand-burgundy); margin-top: 0.3rem; margin-bottom: 0.85rem; padding: 0.25rem 0 0.1rem 0; }
    .register-metrics { display: flex; justify-content: center; gap: 0.7rem; flex-wrap: wrap; }
    .register-metrics span { padding: 0.32rem 0.65rem; border-radius: 999px; background: rgba(120,5,31,0.07); color: #4b3440; font-size: 14px; }
    .article-code { font-weight: 950; white-space: nowrap; color: var(--brand-burgundy); }
    .article-title { color: #b45f6d; font-size: 14.5px; font-weight: 650; }
    .context-meta { margin-top: 0.40rem; padding: 4px 8px; background: rgba(255,247,248,0.98); border-left: 4px solid #dda3aa; border-radius: 8px; display: inline-block; }
    .kwic-wrapper { background: linear-gradient(180deg, rgba(255,253,253,0.985) 0%, rgba(255,248,250,0.96) 100%); border: 1px solid rgba(176,112,44,0.13); border-radius: 15px; padding: 0.75rem 1rem; box-shadow: 0 12px 28px rgba(72,0,18,0.045); }
    .kwic-row { display: grid; grid-template-columns: minmax(0, 42%) minmax(118px, 14%) minmax(0, 42%); gap: 9px; border-bottom: 1px solid rgba(120,5,31,0.10); padding: 8px 0; align-items: start; font-family: "Georgia", "Cambria", "Times New Roman", serif; font-size: 18px; line-height: 1.42; }
    .kwic-left { text-align: right; color: #33282e; }
    .kwic-keyword { text-align: center; font-weight: 900; color: var(--brand-burgundy); background: rgba(120,5,31,0.075); border-radius: 9px; padding: 0 0.4rem; min-width: 118px; }
    .kwic-right { text-align: left; color: #33282e; }

    .corpus-card-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.9rem; margin: 0.7rem 0 1.1rem 0; }
    .corpus-card { background: rgba(255,250,247,0.90); border: 1px solid rgba(176,112,44,0.18); border-radius: 16px; padding: 1.05rem 1rem; text-align: center; box-shadow: 0 10px 24px rgba(72,0,18,0.055); }
    .corpus-card-label { font-size: 13px; font-weight: 850; letter-spacing: 0.09em; text-transform: uppercase; color: #7d4651; }
    .corpus-card-value { margin-top: 0.45rem; font-size: 31px; font-weight: 950; color: var(--brand-burgundy); letter-spacing: 0.03em; }
    div[data-testid="stDataFrame"] div[role="gridcell"], div[data-testid="stDataFrame"] div[role="columnheader"] { text-align: center !important; justify-content: center !important; }

    .about-text, .citation-box { background: rgba(255,250,247,0.92); border: 1px solid rgba(176,112,44,0.18); border-radius: 16px; padding: 1rem 1.15rem; box-shadow: 0 8px 22px rgba(72,0,18,0.045); color: #24171f !important; }
    .about-text, .about-text p, .about-text div, .about-text span, .about-text strong,
    .citation-box, .citation-box p, .citation-box div, .citation-box span, .citation-box strong { color: #24171f !important; }
    .citation-box { color: #4c3440 !important; }
    .site-motto { margin: 1.55rem auto 0.55rem auto; text-align: center; font-family: "Cormorant Garamond", "Georgia", serif; font-style: italic; font-size: 25px; line-height: 1.22; font-weight: 700; color: rgba(255, 242, 214, 0.98); text-shadow: 0 2px 10px rgba(0,0,0,0.30), 0 0 14px rgba(255,223,139,0.12); }
    .site-footer { margin: 0.35rem auto 0.9rem auto; text-align: center; padding: 1rem 1.2rem; border-radius: 15px; background: rgba(255,250,247,0.84); border: 1px solid rgba(176,112,44,0.14); color: #4F3D45; font-size: 15px; box-shadow: 0 8px 20px rgba(72,0,18,0.04); }
    .site-footer strong, .site-footer a { color: var(--brand-burgundy); font-weight: 850; }
    .site-footer a { text-decoration: none; }
    .site-footer a:hover { text-decoration: underline; }



    /* DESKTOP: фіксована преміальна композиція.
       MOBILE: окрема мобільна версія без масштабування всієї сторінки. */
    :root {
        --site-fixed-width: 1320px;
        --site-inner-width: 1300px;
    }

    html, body, .stApp, [data-testid="stAppViewContainer"] {
        width: 100% !important;
        min-width: 0 !important;
        overflow-x: hidden !important;
    }
    [data-testid="stAppViewContainer"] > .main,
    [data-testid="stAppViewContainer"] section.main,
    section[data-testid="stMain"] {
        width: 100% !important;
        min-width: 0 !important;
        overflow-x: hidden !important;
    }

    /* Desktop-версія лишається такою, як була: фіксована, центрована, без роз'їзду. */
    @media (min-width: 821px) {
        .block-container {
            width: var(--site-fixed-width) !important;
            min-width: var(--site-fixed-width) !important;
            max-width: var(--site-fixed-width) !important;
            box-sizing: border-box !important;
            padding-left: 0 !important;
            padding-right: 0 !important;
            margin-left: auto !important;
            margin-right: auto !important;
            position: relative !important;
            left: auto !important;
            right: auto !important;
            transform: none !important;
            transform-origin: top center !important;
            zoom: 1;
        }
        @media (max-width: 1390px) { .block-container { zoom: 0.985; } }
        @media (max-width: 1320px) { .block-container { zoom: 0.94; } }
        @media (max-width: 1240px) { .block-container { zoom: 0.88; } }
        @media (max-width: 1160px) { .block-container { zoom: 0.82; } }
        @media (max-width: 1080px) { .block-container { zoom: 0.76; } }
        @media (max-width: 980px)  { .block-container { zoom: 0.69; } }
        @media (max-width: 900px)  { .block-container { zoom: 0.63; } }

        .hero-panel {
            display: grid !important;
            grid-template-columns: 842px 400px !important;
            gap: 58px !important;
            align-items: flex-start !important;
            width: var(--site-inner-width) !important;
            min-width: var(--site-inner-width) !important;
            max-width: var(--site-inner-width) !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }
        .hero-text {
            width: 842px !important;
            min-width: 842px !important;
            max-width: 842px !important;
        }
        .site-brand {
            font-size: 96px !important;
            white-space: nowrap !important;
            letter-spacing: 0.026em !important;
            line-height: 0.90 !important;
        }
        .site-subtitle {
            font-size: 22px !important;
            line-height: 1.22 !important;
            white-space: nowrap !important;
        }
        .hero-quote {
            font-size: 32px !important;
            max-width: 720px !important;
            white-space: normal !important;
        }
        .hero-portrait-card {
            width: 400px !important;
            min-width: 400px !important;
            height: 415px !important;
            min-height: 415px !important;
        }
        div[data-baseweb="tab-list"] {
            width: var(--site-inner-width) !important;
            min-width: var(--site-inner-width) !important;
            max-width: var(--site-inner-width) !important;
            flex-wrap: nowrap !important;
            overflow: visible !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }
        button[role="tab"] {
            flex: 0 0 auto !important;
            white-space: nowrap !important;
        }
        [data-testid="stForm"] {
            width: var(--site-inner-width) !important;
            min-width: var(--site-inner-width) !important;
            max-width: var(--site-inner-width) !important;
            box-sizing: border-box !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }
        [data-testid="stForm"] textarea { min-width: 0 !important; }
        .kwic-wrapper,
        .register-card,
        .corpus-card-grid,
        .about-text,
        .citation-box,
        .site-footer {
            max-width: var(--site-inner-width) !important;
            margin-left: auto !important;
            margin-right: auto !important;
        }
    }

    /* MOBILE: окрема мобільна версія. Нічого не зменшується як картинка,
       користувач може нормально читати, натискати й збільшувати пальцями. */
    @media (max-width: 820px) {
        html, body, .stApp, [data-testid="stAppViewContainer"] {
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            overflow-x: hidden !important;
            -webkit-text-size-adjust: 100% !important;
            touch-action: pan-x pan-y pinch-zoom !important;
        }
        [data-testid="stAppViewContainer"] > .main,
        [data-testid="stAppViewContainer"] section.main,
        section[data-testid="stMain"] {
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            overflow-x: hidden !important;
        }
        .block-container {
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            padding: 0.82rem 0.82rem 1.35rem 0.82rem !important;
            margin: 0 auto !important;
            left: auto !important;
            right: auto !important;
            transform: none !important;
            zoom: 1 !important;
            box-sizing: border-box !important;
        }

        /* Мобільна шапка: назва → підзаголовок → цитата → світлина. */
        .hero-panel {
            display: flex !important;
            flex-direction: column !important;
            align-items: center !important;
            justify-content: flex-start !important;
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            gap: 0.86rem !important;
            margin: 0 auto 0.95rem auto !important;
            padding: 0.5rem 0 0.6rem 0 !important;
            text-align: center !important;
        }
        .hero-text {
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            padding-top: 0 !important;
            text-align: center !important;
            order: 1 !important;
        }
        .site-brand {
            display: block !important;
            width: 100% !important;
            max-width: 100% !important;
            font-size: clamp(31px, 10.5vw, 58px) !important;
            line-height: 0.98 !important;
            letter-spacing: 0.011em !important;
            white-space: nowrap !important;
            overflow-wrap: normal !important;
            word-break: keep-all !important;
            text-align: center !important;
            margin: 0 auto 0.34rem auto !important;
        }
        .site-subtitle {
            font-size: clamp(14px, 4.05vw, 19px) !important;
            line-height: 1.28 !important;
            white-space: normal !important;
            max-width: min(94vw, 520px) !important;
            margin: 0.15rem auto 0.78rem auto !important;
            text-align: center !important;
        }
        .hero-quote {
            display: block !important;
            font-size: clamp(18px, 5.35vw, 25px) !important;
            line-height: 1.25 !important;
            max-width: min(91vw, 500px) !important;
            padding-left: 2.25rem !important;
            margin: 0 auto !important;
            text-align: left !important;
            white-space: normal !important;
        }
        .hero-quote::before {
            left: 0.46rem !important;
            top: -0.82rem !important;
            font-size: 54px !important;
        }
        .hero-quote::after {
            left: 0 !important;
            top: 0.05rem !important;
            height: calc(100% - 0.05rem) !important;
        }
        .hero-portrait-wrap {
            order: 2 !important;
            display: flex !important;
            justify-content: center !important;
            align-items: flex-start !important;
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            margin: 0 auto !important;
        }
        .hero-portrait-card {
            width: min(70vw, 292px) !important;
            min-width: 0 !important;
            height: auto !important;
            aspect-ratio: 0.96 / 1 !important;
            margin: 0.04rem auto 0 auto !important;
            border-width: 4px !important;
            border-radius: 19px !important;
        }
        .hero-portrait-card img {
            object-position: center 17% !important;
        }

        /* Вкладки на телефоні: адаптивна сітка вкладок без горизонтального гортання. */
        div[data-baseweb="tab-list"] {
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            display: grid !important;
            grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
            grid-auto-rows: minmax(78px, auto) !important;
            gap: 0.36rem !important;
            overflow: visible !important;
            margin: 0.18rem auto 0.92rem auto !important;
            padding: 0 0 0.42rem 0 !important;
            border-bottom: 1.5px solid rgba(235, 198, 111, 0.55) !important;
            box-sizing: border-box !important;
        }
        button[role="tab"] {
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            min-height: 78px !important;
            height: auto !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            padding: 0.52rem 0.24rem 0.5rem 0.24rem !important;
            font-size: clamp(11.4px, 3.35vw, 13.4px) !important;
            border-radius: 13px !important;
            white-space: normal !important;
            overflow: hidden !important;
            line-height: 1.12 !important;
        }
        button[role="tab"]::after { border-radius: 13px !important; }
        button[role="tab"] p {
            display: flex !important;
            flex-direction: column !important;
            align-items: center !important;
            justify-content: center !important;
            gap: 0.27rem !important;
            width: 100% !important;
            max-width: 100% !important;
            margin: 0 !important;
            padding: 0 !important;
            text-align: center !important;
            white-space: normal !important;
            overflow-wrap: normal !important;
            word-break: normal !important;
            line-height: 1.13 !important;
        }
        button[role="tab"] p::before {
            width: 19px !important;
            height: 19px !important;
            margin: 0 auto !important;
        }
        button[role="tab"][aria-selected="true"] {
            padding-left: 0.24rem !important;
            padding-right: 0.24rem !important;
            transform: none !important;
        }

        /* Пошук і розширені налаштування: одна колонка, кнопка на всю ширину. */
        [data-testid="stForm"] {
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            margin: 0.75rem auto 1rem auto !important;
            padding: 0.95rem 0.82rem 1rem 0.82rem !important;
            border-radius: 18px !important;
            box-sizing: border-box !important;
        }
        [data-testid="stForm"] [data-testid="stHorizontalBlock"] {
            display: flex !important;
            flex-direction: column !important;
            flex-wrap: nowrap !important;
            align-items: stretch !important;
            gap: 0.72rem !important;
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
        }
        [data-testid="stForm"] [data-testid="column"] {
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            flex: 1 1 100% !important;
            padding-left: 0 !important;
            padding-right: 0 !important;
        }
        [data-testid="stTextArea"] textarea {
            min-height: 86px !important;
            font-size: 16px !important;
        }
        div[data-testid="stFormSubmitButton"] {
            width: 100% !important;
            justify-content: stretch !important;
            margin-top: 0.02rem !important;
        }
        div[data-testid="stFormSubmitButton"] button {
            width: 100% !important;
            min-height: 52px !important;
            font-size: 17px !important;
        }
        [data-testid="stForm"] [data-testid="stExpander"] {
            width: 100% !important;
            margin-top: 0.55rem !important;
        }
        [data-testid="stForm"] [data-testid="stExpander"] [data-testid="stHorizontalBlock"] {
            display: flex !important;
            flex-direction: column !important;
            gap: 0.72rem !important;
        }
        [data-testid="stForm"] [data-testid="stSelectbox"],
        [data-testid="stForm"] [data-testid="stNumberInput"],
        [data-testid="stForm"] [data-testid="stCheckbox"],
        [data-testid="stForm"] [data-testid="stRadio"] {
            width: 100% !important;
            max-width: 100% !important;
        }

        .register-card,
        .kwic-wrapper,
        .corpus-card-grid,
        .about-text,
        .citation-box,
        .site-footer {
            width: 100% !important;
            min-width: 0 !important;
            max-width: 100% !important;
            margin-left: auto !important;
            margin-right: auto !important;
            box-sizing: border-box !important;
        }
        .register-card {
            padding: 1rem 0.85rem !important;
            border-radius: 18px !important;
            text-align: center !important;
        }
        .register-title,
        .headword {
            font-size: clamp(24px, 7.5vw, 36px) !important;
            line-height: 1.12 !important;
            overflow-wrap: anywhere !important;
            text-align: center !important;
        }
        .register-metrics,
        .register-pills {
            display: flex !important;
            flex-wrap: wrap !important;
            justify-content: center !important;
            gap: 0.45rem !important;
        }
        .kwic-row {
            grid-template-columns: 1fr !important;
            gap: 0.55rem !important;
            padding: 0.8rem 0 !important;
        }
        .kwic-left, .kwic-right, .kwic-keyword {
            text-align: left !important;
            min-width: 0 !important;
        }
        .kwic-keyword { justify-content: flex-start !important; }
        .kwic-keyword span { max-width: 100% !important; }
        .corpus-card-grid {
            grid-template-columns: 1fr !important;
            gap: 0.65rem !important;
        }

        [data-testid="stDataFrame"],
        .stDataFrame,
        div[data-testid="stTable"],
        div[data-testid="stDataFrame"] > div {
            width: 100% !important;
            max-width: 100% !important;
            overflow-x: auto !important;
            -webkit-overflow-scrolling: touch !important;
        }
        .stDownloadButton, .stDownloadButton button,
        div[data-testid="stDownloadButton"], div[data-testid="stDownloadButton"] button,
        div.stButton, div.stButton > button {
            width: 100% !important;
            max-width: 100% !important;
        }
        .site-motto { font-size: 21px !important; }
        .site-footer { font-size: 14px !important; padding: 0.9rem 0.85rem !important; }
    }

    @media (max-width: 360px) {
        .block-container { padding-left: 0.65rem !important; padding-right: 0.65rem !important; }
        .site-brand { font-size: clamp(29px, 10.1vw, 38px) !important; letter-spacing: 0.006em !important; }
        div[data-baseweb="tab-list"] { gap: 0.26rem !important; grid-auto-rows: minmax(74px, auto) !important; }
        button[role="tab"] { min-height: 74px !important; font-size: 11px !important; padding-left: 0.12rem !important; padding-right: 0.12rem !important; }
        button[role="tab"] p::before { width: 18px !important; height: 18px !important; }
        .hero-portrait-card { width: min(76vw, 270px) !important; }
    }

    </style>
    """,
    unsafe_allow_html=True,
)

portrait_html = ""
if PORTRAIT_DATA_URI:
    portrait_html = (
        "<div class='hero-portrait-wrap'>"
        f"<div class='hero-portrait-card'><img src='{PORTRAIT_DATA_URI}' alt='Степан Бандера'></div>"
        "</div>"
    )

st.markdown(
    "<div class='hero-panel'>"
    "<div class='hero-text'>"
    "<div class='site-brand'>БАНДЕРОГРАФІЯ</div>"
    "<div class='site-subtitle'>Лінгвістичний портал</div>"
    "<div class='hero-quote'>Змістом мойого життя досі була боротьба,<br>так і мусить бути дальше</div>"
    "</div>"
    f"{portrait_html}"
    "</div>",
    unsafe_allow_html=True,
)

if not DB_PATH.exists():
    st.error(
        "Не знайдено outputs/concordance_index.sqlite. Спочатку в PyCharm запусти: "
        ".\\venv\\Scripts\\python.exe main.py --rebuild --mode sentence"
    )
    st.stop()

STAMP = db_stamp()
wordforms_df = load_wordforms(STAMP)
articles_df = load_articles(STAMP)


search_tab, freq_tab, corpus_tab, articles_tab, dictionary_tab, variants_tab, about_tab = st.tabs([
    "Конкорданс",
    "Частотний словник",
    "Корпус",
    "Статті корпусу",
    "Словник",
    "Варіянтні слова",
    "Про сайт",
])

with search_tab:
    with st.form("search_form"):
        main_col, select_col, button_col = st.columns([3.25, 2.2, 1.18], vertical_alignment="bottom")
        alpha_wordform_options = sorted(wordforms_df["wordform"].dropna().astype(str).unique().tolist(), key=str.casefold)
        with main_col:
            query_text = st.text_area(
                "Словоформа / словоформи для пошуку",
                placeholder="Наприклад: України, революції, ОУН",
                height=62,
                help="Введи одну або кілька словоформ і натисни кнопку «Пошук». Можна розділяти словоформи комами, крапками з комою або новими рядками.",
            )
        with select_col:
            selected_alpha_wordform = st.selectbox(
                "Або виберіть словоформу за абеткою",
                options=["— не вибрано —"] + alpha_wordform_options,
                index=0,
                key="concordance_alpha_wordform_select",
            )
        with button_col:
            search_clicked = st.form_submit_button("Пошук", type="primary")

        with st.expander("Розширені налаштування", expanded=True):
            col1, col2, col3, col4 = st.columns([1.55, 1.05, 1.05, 1.35])
            with col1:
                mode = st.selectbox(
                    "Режим контексту",
                    ["Реченнєвий контекст", "Контекст фіксованої глибини"],
                    index=0,
                )
            with col2:
                depth = st.number_input("Глибина", min_value=1, max_value=50, value=7, step=1)
            with col3:
                max_rows = st.number_input("Показати прикладів", min_value=20, max_value=1000, value=150, step=20)
            with col4:
                variants_choice = st.selectbox("Об’єднувати варіянти", ["Так", "Ні"], index=0)
                use_variants = variants_choice == "Так"

    random_candidates = [w for w in wordforms_df["wordform"].dropna().astype(str).tolist() if w.strip()]
    if random_candidates:
        if "concordance_random_wordforms" not in st.session_state:
            st.session_state["concordance_random_wordforms"] = random.sample(random_candidates, min(8, len(random_candidates)))
        st.markdown("<div class='random-wordforms-title'>Випадкові словоформи з корпусу</div>", unsafe_allow_html=True)
        with keyed_container("random_wordforms_row"):
            for idx, wf in enumerate(st.session_state.get("concordance_random_wordforms", [])[:8]):
                if st.button(wf, key=f"random_wordform_{idx}_{wf}"):
                    st.session_state["last_search"] = {
                        "queries": parse_queries(wf),
                        "mode": mode,
                        "depth": int(depth),
                        "use_variants": bool(use_variants),
                        "max_rows": int(max_rows),
                        "selected_codes": [],
                    }
                    fast_rerun()

    if search_clicked:
        chosen_query = query_text.strip()
        if not chosen_query and selected_alpha_wordform != "— не вибрано —":
            chosen_query = selected_alpha_wordform
        st.session_state["last_search"] = {
            "queries": parse_queries(chosen_query),
            "mode": mode,
            "depth": int(depth),
            "use_variants": bool(use_variants),
            "max_rows": int(max_rows),
            "selected_codes": [],
        }

    search_state = st.session_state.get("last_search")

    if not search_state:
        st.markdown("<div style='height: 0.25rem;'></div>", unsafe_allow_html=True)
    else:
        queries = search_state["queries"]
        current_mode = search_state["mode"]
        current_depth = int(search_state["depth"])
        current_use_variants = bool(search_state["use_variants"])
        current_max_rows = int(search_state["max_rows"])
        found_wordforms = get_wordforms_for_queries(queries, current_use_variants, wordforms_df)

        if queries and not found_wordforms:
            st.warning("За цим запитом словоформ не знайдено.")
        elif not queries:
            st.info("Введи одну або кілька словоформ і натисни кнопку «Пошук».")
        else:
            available_codes_df = fetch_available_codes(STAMP, tuple(found_wordforms))
            code_options = available_codes_df["code"].tolist() if not available_codes_df.empty else []
            code_labels = {
                row.code: f"{row.code} — {row.title} ({int(row.hits)})"
                for row in available_codes_df.itertuples(index=False)
            }

            selected_codes = st.multiselect(
                "Фільтр за кодом статті",
                options=code_options,
                default=st.session_state.get("selected_codes_current", []),
                format_func=lambda c: code_labels.get(c, c),
                help="Список містить лише ті статті, у яких уживаються знайдені словоформи.",
            )
            st.session_state["selected_codes_current"] = selected_codes

            total_hits = count_contexts(STAMP, tuple(found_wordforms), tuple(selected_codes))
            header = format_register_header(found_wordforms, wordforms_df, current_use_variants)
            st.markdown(
                render_register_card(header, total_hits, len(available_codes_df), current_mode, current_depth),
                unsafe_allow_html=True,
            )

            if not available_codes_df.empty:
                with st.expander("Розподіл знайдених слововживань за статтями", expanded=False):
                    breakdown_df = fetch_article_counts_for_wordforms(STAMP, tuple(found_wordforms))
                    if selected_codes:
                        breakdown_df = breakdown_df[breakdown_df["code"].isin(selected_codes)]
                    breakdown_view = breakdown_df.rename(columns={
                        "code": "Код статті",
                        "title": "Назва статті",
                        "frequency": "Кількість слововживань",
                    })
                    st.dataframe(breakdown_view, width="stretch", hide_index=True)
                    st.download_button(
                        "⬇️ Завантажити розподіл Excel",
                        data=dataframe_to_xlsx_bytes({"Розподіл": breakdown_view}),
                        file_name="wordform_article_distribution.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )

            result_df = fetch_contexts(
                STAMP,
                tuple(found_wordforms),
                current_mode,
                current_depth,
                tuple(selected_codes),
                current_max_rows,
            )

            if result_df.empty:
                st.warning("Після фільтрації контекстів не залишилося.")
            else:
                st.markdown(render_kwic_html(result_df), unsafe_allow_html=True)
                if total_hits > len(result_df):
                    st.markdown(
                        f"<div class='limit-info-note'>Показано {len(result_df)} прикладів із {total_hits}. "
                        f"Збільш кількість прикладів або підготуй повний файл.</div>",
                        unsafe_allow_html=True,
                    )

                shown_export = concordance_export_df(result_df)
                st.markdown("<div class='concordance-download-spacer'></div>", unsafe_allow_html=True)
                dl1, dl2 = st.columns([1, 1])
                with dl1:
                    st.download_button(
                        "⬇️ Завантажити показані контексти CSV",
                        data=shown_export.to_csv(index=False, sep=";").encode("utf-8-sig"),
                        file_name="contexts_shown.csv",
                        mime="text/csv",
                    )
                with dl2:
                    st.download_button(
                        "⬇️ Завантажити показані контексти Excel",
                        data=dataframe_to_xlsx_bytes({"Контексти": shown_export}),
                        file_name="contexts_shown.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )

                prepare_full_csv = st.checkbox(
                    "Підготувати повний файл для цього запиту",
                    value=False,
                    help="Для дуже частотних словоформ це може зайняти кілька секунд, тому повний файл готується тільки за потреби.",
                )
                if prepare_full_csv:
                    full_limit = min(total_hits, 50000)
                    full_df = fetch_contexts(
                        STAMP,
                        tuple(found_wordforms),
                        current_mode,
                        current_depth,
                        tuple(selected_codes),
                        full_limit,
                    )
                    full_export = concordance_export_df(full_df)
                    fd1, fd2 = st.columns([1, 1])
                    with fd1:
                        st.download_button(
                            "⬇️ Завантажити повний CSV",
                            data=full_export.to_csv(index=False, sep=";").encode("utf-8-sig"),
                            file_name="contexts_full.csv",
                            mime="text/csv",
                        )
                    with fd2:
                        st.download_button(
                            "⬇️ Завантажити повний Excel",
                            data=dataframe_to_xlsx_bytes({"Контексти": full_export}),
                            file_name="contexts_full.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        )
                    if total_hits > full_limit:
                        st.warning(f"Файл обмежено першими {full_limit} контекстами, щоб не перевантажувати сайт.")

with freq_tab:
    st.subheader("Частотний словник")
    freq_scope = st.radio(
        "Тип частотного списку",
        ["Загальний частотний список", "Частотний список окремої статті"],
        horizontal=True,
        key="freq_scope_radio",
    )

    if freq_scope == "Частотний список окремої статті":
        article_label = {
            int(r.article_id): f"{r.code}: {r.title}"
            for r in articles_df.itertuples(index=False)
        }
        selected_article_for_freq = st.selectbox(
            "Оберіть статтю",
            options=articles_df["article_id"].tolist(),
            format_func=lambda article_id: article_label.get(int(article_id), str(article_id)),
            key="freq_article_select",
        )
        source_freq_df = fetch_article_frequency(STAMP, int(selected_article_for_freq))
    else:
        source_freq_df = wordforms_df[["wordform", "frequency"]].copy()

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        freq_query = st.text_input("Пошук у частотному списку", placeholder="Наприклад: РЕВОЛЮЦ", key="freq_query_input")
    with c2:
        min_frequency = st.number_input("Мінімальна частота", min_value=1, value=1, step=1, key="freq_min_frequency")
    with c3:
        top_n = st.number_input("Кількість рядків", min_value=10, max_value=50000, value=500, step=50, key="freq_top_n")

    freq_view = source_freq_df[source_freq_df["frequency"] >= int(min_frequency)].copy()
    if freq_query:
        q = normalize_wordform(freq_query)
        freq_view = freq_view[freq_view["wordform"].astype(str).str.contains(re.escape(q), regex=True, na=False)]
    freq_view_ua = freq_view[["wordform", "frequency"]].rename(columns={
        "wordform": "Словоформа",
        "frequency": "Частота",
    })
    st.dataframe(freq_view_ua.head(int(top_n)), width="stretch", hide_index=True)
    fd1, fd2 = st.columns([1, 1])
    with fd1:
        st.download_button(
            "⬇️ Завантажити частотний список CSV",
            data=freq_view_ua.to_csv(index=False, sep=";").encode("utf-8-sig"),
            file_name="frequency_list.csv",
            mime="text/csv",
        )
    with fd2:
        st.download_button(
            "⬇️ Завантажити частотний список Excel",
            data=dataframe_to_xlsx_bytes({"Частотний список": freq_view_ua}),
            file_name="frequency_list.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

with corpus_tab:
    st.subheader("Паспорт корпусу")
    total_articles = int(len(articles_df))
    total_tokens = int(articles_df["tokens"].sum()) if not articles_df.empty else 0
    unique_wordforms = int(len(wordforms_df)) if not wordforms_df.empty else 0
    variants_df_for_stats = load_variant_groups(STAMP)
    variant_groups_n = int(len(variants_df_for_stats)) if not variants_df_for_stats.empty else 0
    years = []
    for code in articles_df.get("code", pd.Series(dtype=str)).astype(str):
        m = re.search(r"(\d{4})", code)
        if m:
            years.append(int(m.group(1)))
    year_range = f"{min(years)}–{max(years)}" if years else "—"

    st.markdown(
        f"""
        <div class='corpus-card-grid'>
            <div class='corpus-card'><div class='corpus-card-label'>Статей</div><div class='corpus-card-value'>{total_articles}</div></div>
            <div class='corpus-card'><div class='corpus-card-label'>Слововживань</div><div class='corpus-card-value'>{total_tokens}</div></div>
            <div class='corpus-card'><div class='corpus-card-label'>Унікальних словоформ</div><div class='corpus-card-value'>{unique_wordforms}</div></div>
            <div class='corpus-card'><div class='corpus-card-label'>Хронологія</div><div class='corpus-card-value'>{year_range}</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    passport_df = pd.DataFrame([
        {"Параметр": "Назва ресурсу", "Опис": "БАНДЕРОГРАФІЯ"},
        {"Параметр": "Тип ресурсу", "Опис": "електронний корпусно-конкордансний ресурс"},
        {"Параметр": "Матеріял", "Опис": "твори-першодруки Степана Бандери"},
        {"Параметр": "Одиниця індексації", "Опис": "словоформа"},
        {"Параметр": "Паспортизація", "Опис": "код статті, назва статті, позиція словоформи"},
        {"Параметр": "Контексти", "Опис": "реченнєвий контекст і контекст фіксованої глибини"},
        {"Параметр": "Варіянтність", "Опис": "окремі правописні й морфологічні варіянти групуються без зміни оригінального тексту"},
        {"Параметр": "Призначення", "Опис": "підготовка матеріялу для словника мови Степана Бандери"},
    ])
    passport_styler = passport_df.style.set_properties(**{"text-align": "center"}).set_table_styles([
        {"selector": "th", "props": [("text-align", "center")]},
        {"selector": "td", "props": [("text-align", "center")]},
    ])
    st.dataframe(passport_styler, width="stretch", hide_index=True)
    st.download_button(
        "⬇️ Завантажити паспорт корпусу Excel",
        data=dataframe_to_xlsx_bytes({"Паспорт корпусу": passport_df}),
        file_name="corpus_passport.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.markdown("### Найчастотніші словоформи корпусу")
    top_corpus = wordforms_df[["wordform", "frequency"]].head(30).rename(columns={
        "wordform": "Словоформа",
        "frequency": "Частота",
    })
    st.dataframe(top_corpus, width="stretch", hide_index=True)

with articles_tab:
    st.metric("Кількість статей", len(articles_df))

    search_article = st.text_input("Пошук статті за кодом або назвою", placeholder="Наприклад: УНР-1950 або революція", key="article_search_input")
    view_meta = articles_df.copy()
    if search_article:
        q = search_article.casefold()
        view_meta = view_meta[
            view_meta["code"].astype(str).str.casefold().str.contains(q, regex=False)
            | view_meta["title"].astype(str).str.casefold().str.contains(q, regex=False)
        ]

    report_view = view_meta[["code", "title", "tokens"]].rename(columns={
        "code": "Код статті",
        "title": "Назва статті",
        "tokens": "Кількість слів",
    })
    st.dataframe(report_view, width="stretch", hide_index=True)

    ad1, ad2 = st.columns([1, 1])
    with ad1:
        st.download_button(
            "⬇️ Завантажити звіт про статті CSV",
            data=report_view.to_csv(index=False, sep=";").encode("utf-8-sig"),
            file_name="articles_report.csv",
            mime="text/csv",
        )
    with ad2:
        st.download_button(
            "⬇️ Завантажити звіт про статті Excel",
            data=dataframe_to_xlsx_bytes({"Статті корпусу": report_view}),
            file_name="articles_report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    st.markdown("### Перегляд і завантаження окремої статті")
    if view_meta.empty:
        st.info("За цим фільтром статей не знайдено.")
    else:
        article_options = view_meta["article_id"].tolist()
        article_label = {
            int(r.article_id): f"{r.code}: {r.title}"
            for r in view_meta.itertuples(index=False)
        }
        selected_article_id = st.selectbox(
            "Оберіть статтю",
            options=article_options,
            format_func=lambda article_id: article_label.get(int(article_id), str(article_id)),
            key="article_text_select",
        )
        selected_article = get_article_text(STAMP, int(selected_article_id))
        if selected_article:
            st.download_button(
                "Завантажити вибрану статтю",
                data=f"{selected_article['code']}\n{selected_article['title']}\n\n{selected_article['text']}".encode("utf-8-sig"),
                file_name=re.sub(r"[\\/:*?\"<>|]+", "_", f"{selected_article['code']} {selected_article['title']}.txt"),
                mime="text/plain",
            )
            with st.expander("Показати текст вибраної статті"):
                st.text(selected_article["text"])

    prepare_zip = st.checkbox(
        "Підготувати ZIP з усіма статтями",
        value=False,
        help="ZIP формується тільки після ввімкнення цієї опції, щоб сайт не робив зайвої роботи на кожному оновленні.",
        key="articles_prepare_zip",
    )
    if prepare_zip:
        all_texts = load_all_article_texts(STAMP)
        if search_article:
            q = search_article.casefold()
            all_texts = all_texts[
                all_texts["code"].astype(str).str.casefold().str.contains(q, regex=False)
                | all_texts["title"].astype(str).str.casefold().str.contains(q, regex=False)
            ]
        st.download_button(
            "⬇️ Завантажити всі вибрані статті ZIP",
            data=make_articles_zip(all_texts),
            file_name="bandera_articles.zip",
            mime="application/zip",
        )

with dictionary_tab:
    render_dictionary_tab(wordforms_df)


with variants_tab:
    st.subheader("Варіянтні слова")
    variants_df = load_variant_groups(STAMP)
    excluded_items = load_excluded_variant_groups()

    if variants_df.empty:
        st.info("Варіянтних слів із двома або більше словоформами не знайдено або всі сумнівні групи вилучено.")
    else:
        type_options = extract_variant_type_options(variants_df)
        v1, v2 = st.columns([2, 2])
        with v1:
            vq = st.text_input("Пошук у варіянтних словах", placeholder="Наприклад: БОЄВИЙ, РЕЖИМ, ДИПЛОМ", key="variants_query_input")
        with v2:
            selected_variant_types = st.multiselect(
                "Фільтр за типом варіянтности",
                options=type_options,
                default=[],
                placeholder="Оберіть типи варіянтности",
            )
        vv = variants_df.copy()
        if vq:
            q = normalize_wordform(vq)
            vv = vv[
                vv["variant_codes"].astype(str).str.contains(re.escape(q), regex=True, na=False)
                | vv["variants"].astype(str).str.contains(re.escape(q), regex=True, na=False)
                | vv["variant_key"].astype(str).str.contains(re.escape(q), regex=True, na=False)
            ]
        if selected_variant_types:
            pattern = "|".join(re.escape(t) for t in selected_variant_types)
            vv = vv[vv["variant_codes"].astype(str).str.contains(pattern, regex=True, na=False)]

        st.markdown("#### Перегляд варіянтних груп")
        vv_ua = vv[["variant_codes", "variants", "total_frequency"]].rename(columns={
            "variant_codes": "Тип варіянтности",
            "variants": "Словоформи й частоти",
            "total_frequency": "Абсолютна частота",
        })
        st.dataframe(vv_ua, width="stretch", hide_index=True)

        st.markdown("#### Вилучення помилкових пар / груп")
        st.caption("Якщо група не є справжньою варіянтною парою, вилучи її. Вибір збережеться у файлі outputs/excluded_variant_groups.json і надалі ця група не братиме участи у варіянтному пошуку.")
        if vv.empty:
            st.info("За поточним фільтром немає груп для вилучення.")
        else:
            exclude_options = vv["signature"].tolist()
            exclude_labels = dict(zip(vv["signature"], vv["variants"]))
            selected_exclude_signature = st.selectbox(
                "Оберіть групу, яку треба вилучити",
                options=exclude_options,
                format_func=lambda sig: exclude_labels.get(sig, sig),
                key="variant_exclude_select",
            )
            if st.button("Вилучити цю групу з варіянтів", key="variant_exclude_button"):
                row = vv[vv["signature"].eq(selected_exclude_signature)].iloc[0]
                add_excluded_variant_group(row)
                st.cache_data.clear()
                st.success("Групу вилучено й збережено. Вона більше не вважатиметься варіянтною групою.")
                fast_rerun()

        if excluded_items:
            with st.expander("Вилучені групи", expanded=False):
                excluded_df = pd.DataFrame(excluded_items)
                view_excluded = excluded_df[["variant_codes", "variants"]].rename(columns={
                    "variant_codes": "Тип варіянтности",
                    "variants": "Вилучена група",
                })
                st.dataframe(view_excluded, width="stretch", hide_index=True)
                restore_signature = st.selectbox(
                    "Повернути вилучену групу",
                    options=excluded_df["signature"].tolist(),
                    format_func=lambda sig: dict(zip(excluded_df["signature"], excluded_df["variants"])).get(sig, sig),
                    key="variant_restore_select",
                )
                if st.button("Повернути цю групу до варіянтів", key="variant_restore_button"):
                    restore_excluded_variant_group(restore_signature)
                    st.cache_data.clear()
                    st.success("Групу повернено до варіянтних слів.")
                    fast_rerun()

        vd1, vd2 = st.columns([1, 1])
        with vd1:
            st.download_button(
                "⬇️ Завантажити варіянтні слова CSV",
                data=vv_ua.to_csv(index=False, sep=";").encode("utf-8-sig"),
                file_name="variant_words_report.csv",
                mime="text/csv",
            )
        with vd2:
            st.download_button(
                "⬇️ Завантажити варіянтні слова Excel",
                data=dataframe_to_xlsx_bytes({"Варіянтні слова": vv_ua}),
                file_name="variant_words_report.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

with about_tab:
    st.markdown(
        """
        <div class='about-text'>
        <p><strong>«БАНДЕРОГРАФІЯ»</strong> — електронний корпусно-конкордансний ресурс,
        створений для дослідження творів-першодруків Степана Бандери. Його призначення — забезпечити
        системний доступ до слововживання автора, частотної організації корпусу, контекстів уживання
        словоформ і варіянтних реєстрових груп.</p>

        <p>Конкорданс дає змогу працювати з матеріялом у двох режимах. У режимі реченнєвого контексту
        лівий і правий контекст обмежені межами речення, що зручно для аналізу синтаксичного та
        семантичного оточення словоформи. У режимі фіксованої глибини користувач сам визначає кількість
        слів ліворуч і праворуч від шуканої одиниці, що наближує подання до класичного KWIC-конкордансу.</p>

        <p>Кожне слововживання паспортизовано кодом статті. Це дає змогу зіставляти словоформи з конкретними
        текстами корпусу, перевіряти частотність у різних статтях, виявляти тематично значущі контексти
        та простежувати лексико-семантичні зв’язки в межах авторського мовлення.</p>

        <p>Окрему увагу приділено варіянтності. Ресурс групує окремі правописні та морфологічні варіянти,
        зберігаючи при цьому оригінальне написання словоформ у контекстах. Це важливо для коректного опису
        мови першодруків і для майбутнього лексикографічного моделювання словника мови Степана Бандери.</p>

        <p>У лексикографічному аспекті ресурс може бути використаний не лише як пошуковий інструмент,
        а і як основа для укладання словника мови Степана Бандери: добору реєстру, перевірки частотности,
        аналізу варіянтности, встановлення типових контекстів і підготовки ілюстративного матеріялу
        для словникових статей.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # =========================
    # Як цитувати сайт
    # =========================

    st.markdown(
        """
        <style>
        .citation-box {
            width: min(100%, 980px);
            box-sizing: border-box;
            margin: 18px auto 0 auto;
            padding: 18px 20px;
            border-radius: 18px;
            border: 1px solid rgba(143, 101, 42, 0.35);
            background: rgba(255, 252, 244, 0.92);
            color: #3b1717;
            font-size: 18px;
            line-height: 1.65;
            text-align: center;
            box-shadow: 0 10px 28px rgba(44, 0, 0, 0.12);
        }

        .citation-box strong {
            font-weight: 850;
        }

        .citation-label {
            font-weight: 850;
            color: #6f0f17;
        }

        .citation-url-label {
            font-weight: 750;
        }

        .citation-box a,
        .citation-box a:visited {
            color: #6f0f17 !important;
            font-weight: 850;
            text-decoration: underline;
            text-underline-offset: 3px;
            overflow-wrap: anywhere;
            word-break: break-word;
        }

        .citation-box a:hover {
            text-decoration-thickness: 2px;
        }

        @media (max-width: 768px) {
            .citation-box {
                width: 94%;
                margin: 14px auto 0 auto;
                padding: 15px 14px;
                font-size: 15.5px;
                line-height: 1.55;
                border-radius: 15px;
            }
        }
        </style>

        <div class="citation-box">
            <span class="citation-label">Як цитувати сайт:</span>
            Кривенок В.
            <strong>БАНДЕРОГРАФІЯ: електронний конкорданс творів-першодруків Степана Бандери</strong>.
            Київ, 2026.
            <br>
            <span class="citation-url-label">URL:</span>
            <a class="citation-link"
               href="https://banderografia.streamlit.app/"
               target="_blank"
               rel="noopener noreferrer">
               https://banderografia.streamlit.app/
            </a>.
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown(
    """
    <div class='site-motto'>Слава Нації!<br>Смерть ворогам!</div>
    <div class='site-footer'>
        <strong>БАНДЕРОГРАФІЯ</strong> · електронний конкорданс творів-першодруків Степана Бандери<br>
        Автор і розробник: <strong>Владислав Кривенок</strong> ·
        <a href="mailto:kryvenokvladyslav@gmail.com">kryvenokvladyslav@gmail.com</a>
    </div>
    """,
    unsafe_allow_html=True,
)
