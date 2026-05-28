# -*- coding: utf-8 -*-
"""Оптимізований Streamlit-інтерфейс електронного конкордансу творів-першодруків Степана Бандери."""

from __future__ import annotations

import base64
import html
import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path
from typing import Iterable

import pandas as pd
import streamlit as st

from variant_rules import normalize_wordform, variant_key, variant_group_codes

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
    button[role="tab"]:nth-of-type(5) p { --tab-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.0' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M7 7h10M17 7l-3-3M17 7l-3 3M17 17H7M7 17l3-3M7 17l3 3'/%3E%3C/svg%3E"); }
    button[role="tab"]:nth-of-type(6) p { --tab-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.0' stroke-linecap='round'%3E%3Ccircle cx='12' cy='12' r='9'/%3E%3Cpath d='M12 10v6M12 7h.01'/%3E%3C/svg%3E"); }

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

        /* Вкладки на телефоні: 6 вкладок = 2 рядки по 3, без горизонтального гортання. */
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
    "<div class='site-subtitle'>Електронний конкорданс творів-першодруків Степана Бандери</div>"
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


search_tab, freq_tab, corpus_tab, articles_tab, variants_tab, about_tab = st.tabs([
    "Конкорданс",
    "Частотний словник",
    "Корпус",
    "Статті корпусу",
    "Варіянтні слова",
    "Про сайт",
])

with search_tab:
    with st.form("search_form"):
        main_col, button_col = st.columns([5.45, 1.18], vertical_alignment="bottom")
        with main_col:
            query_text = st.text_area(
                "Словоформа / словоформи для пошуку",
                placeholder="Наприклад: України, революції, ОУН",
                height=62,
                help="Введи одну або кілька словоформ і натисни кнопку «Пошук». Можна розділяти словоформи комами, крапками з комою або новими рядками.",
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
                st.markdown("<div class='advanced-note'>Для варіянтних слів зберігається оригінальне написання в контексті.</div>", unsafe_allow_html=True)

    if search_clicked:
        st.session_state["last_search"] = {
            "queries": parse_queries(query_text),
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
                st.rerun()

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
                    st.rerun()

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
