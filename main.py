# -*- coding: utf-8 -*-
"""
Побудова електронного конкордансу творів-першодруків Степана Бандери.

Основні можливості:
- читання корпусу з DOCX;
- автоматичне зіставлення статей зі списком кодів;
- побудова SQLite-індексу;
- збереження розділових знаків у контекстах;
- експорт частотного списку, звіту про статті, звіту про варіянтні групи;
- експорт повного конкордансу в CSV/HTML у режимі речення або фіксованої глибини.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import os
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from docx import Document

from variant_rules import normalize_wordform, variant_key, variant_group_codes

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data" / "source"
OUTPUTS_DIR = PROJECT_DIR / "outputs"
CACHE_DIR = PROJECT_DIR / "cache"
DB_PATH = OUTPUTS_DIR / "concordance_index.sqlite"
HASH_PATH = CACHE_DIR / "source_hash.txt"

EMPTY_CONTEXT = "------"

# Українські літери + латиниця для скорочень типу ОУН, ЗЧ, Z тощо.
WORD_RE = re.compile(
    r"[A-Za-zА-Яа-яІіЇїЄєҐґ]+(?:[’'`ʼʹ‘՚-][A-Za-zА-Яа-яІіЇїЄєҐґ]+)*",
    re.UNICODE,
)
SENTENCE_END_RE = re.compile(r"[.!?…]+[\)\]\}»”’']*\s+|\n+")
SPACES_RE = re.compile(r"\s+")


@dataclass
class ArticleCode:
    order: int
    code: str
    title: str
    bibliography: str = ""


@dataclass
class Article:
    article_id: int
    code: str
    title: str
    text: str


@dataclass
class Token:
    article_id: int
    sentence_id: int
    token_index: int
    wordform: str
    norm_wordform: str
    var_key: str
    char_start: int
    char_end: int
    sentence_start: int
    sentence_end: int


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def normalize_title(text: str) -> str:
    """
    Нормалізує назви статей для зіставлення з кодами.

    Окремо прибирає службові дужкові примітки в заголовках, наприклад:
    СУЧАСНИЙ МІЖНАРОДНІЙ СТАН І НАШІ ЗАВДАННЯ («ШП»)
    -> СУЧАСНИЙ МІЖНАРОДНІЙ СТАН І НАШІ ЗАВДАННЯ

    Це потрібно, бо в корпусі біля деяких назв можуть бути редакторські
    позначки джерела / видання, яких немає у списку кодів статей.
    """
    text = str(text or "")

    # Прибираємо службові дужкові примітки: («ШП»), (ШП), [ШП], {ШП}.
    # Такі примітки не є власне назвою статті, тому не повинні заважати
    # добору коду зі списку статей.
    text = re.sub(r"\s*\([^)]{1,80}\)", " ", text)
    text = re.sub(r"\s*\[[^\]]{1,80}\]", " ", text)
    text = re.sub(r"\s*\{[^}]{1,80}\}", " ", text)

    text = normalize_wordform(text)
    text = text.replace("’", "")
    text = re.sub(r"[^A-ZА-ЯІЇЄҐ0-9]+", " ", text)
    text = SPACES_RE.sub(" ", text).strip()
    return text


def clean_text_fragment(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = text.replace("\u200b", "")
    text = text.replace("\ufeff", "")
    text = text.replace("\ufffe", "")
    text = text.replace("￾", "")
    text = SPACES_RE.sub(" ", text)
    return text.strip()


def read_docx_paragraphs(path: Path) -> list[tuple[str, str]]:
    doc = Document(path)
    result: list[tuple[str, str]] = []
    for p in doc.paragraphs:
        text = clean_text_fragment(p.text)
        if not text:
            continue
        style_name = ""
        try:
            style_name = p.style.name or ""
        except Exception:
            style_name = ""
        result.append((text, style_name))
    return result


def find_source_files() -> tuple[Path, Path]:
    docx_files = sorted(DATA_DIR.glob("*.docx"))
    if not docx_files:
        raise FileNotFoundError(
            f"У папці {DATA_DIR} немає DOCX-файлів. Поклади туди файл із текстами "
            f"і файл 'Список статей Степана Бандери.docx'."
        )

    list_candidates = [p for p in docx_files if "список" in p.name.lower() and "стат" in p.name.lower()]
    if not list_candidates:
        raise FileNotFoundError("Не знайдено файл зі списком статей. У назві має бути 'Список статей'.")
    list_path = list_candidates[0]

    corpus_candidates = [p for p in docx_files if p != list_path]
    if not corpus_candidates:
        raise FileNotFoundError("Не знайдено файл корпусу з текстами Степана Бандери.")

    # Якщо файлів кілька, беремо найбільший як корпус.
    corpus_path = max(corpus_candidates, key=lambda p: p.stat().st_size)
    return corpus_path, list_path


def parse_article_codes(list_path: Path) -> list[ArticleCode]:
    """
    Зчитує список кодів статей.

    Підтримує обидва формати:
    1. МЖД-1959: Мої життєписні дані
    МЖД-1959: Мої життєписні дані

    Тобто нумерація на початку рядка необов’язкова. Це важливо, бо Word
    може зберігати автоматичну нумерацію не як частину тексту абзацу.
    """
    paragraphs = [text for text, _ in read_docx_paragraphs(list_path)]
    full = "\n".join(paragraphs)

    # Формат із необов’язковою нумерацією:
    # 1. МЖД-1959: Мої життєписні дані
    # або
    # МЖД-1959: Мої життєписні дані
    pattern = re.compile(
        r"(?m)^\s*(?:(\d+)\.\s*)?([A-Za-zА-Яа-яІіЇїЄєҐґ0-9іІвВ]+(?:[іІиИ]?[A-Za-zА-Яа-яІіЇїЄєҐґ0-9]+)*-[0-9]{4})\s*:\s*(.+?)\s*$"
    )

    matches = list(pattern.finditer(full))
    codes: list[ArticleCode] = []
    for i, m in enumerate(matches):
        order = int(m.group(1)) if m.group(1) else len(codes) + 1
        code = clean_text_fragment(m.group(2))
        title = clean_text_fragment(m.group(3))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full)
        bibliography = clean_text_fragment(full[start:end])
        codes.append(ArticleCode(order=order, code=code, title=title, bibliography=bibliography))

    if not codes:
        raise ValueError(
            "Не вдалося зчитати коди статей зі списку. Підтримуваний формат: "
            "'1. КОД-РІК: Назва' або 'КОД-РІК: Назва'."
        )
    return codes


def is_heading_style(style_name: str) -> bool:
    style = (style_name or "").lower()
    return "heading" in style or "заголов" in style


def is_numbered_subheading(text: str) -> bool:
    """
    Внутрішні підрозділи великої статті, наприклад:
    1. Необхідність національно-визвольної революційної боротьби
    не повинні ставати окремими статтями корпусу.
    """
    return bool(re.match(r"^\s*\d+[.)]\s+", text.strip()))


def is_all_caps_heading(text: str) -> bool:
    """
    Дозволяє впізнати основні заголовки статей, яких немає у списку кодів
    наприклад, ФРОНТ ПОНЕВОЛЕНИХ НАЦІЙ.
    Водночас не приймає звичайні внутрішні підзаголовки типу
    "1. Необхідність...".
    """
    letters = re.findall(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]", text)
    if not letters:
        return False
    upper_letters = [ch for ch in letters if ch.upper() == ch]
    return len(upper_letters) / len(letters) >= 0.85


def is_probable_article_title(text: str, style_name: str, known_titles: set[str]) -> bool:
    norm = normalize_title(text)
    if not norm:
        return False

    # Внутрішні нумеровані підрозділи НЕ є окремими статтями.
    if is_numbered_subheading(text):
        return False

    # Основний і найнадійніший критерій: назва є у списку статей.
    if norm in known_titles:
        return True

    # Додатковий критерій тільки для справжніх основних заголовків, яких немає
    # у списку кодів. Так можна не втратити статтю "ФРОНТ ПОНЕВОЛЕНИХ НАЦІЙ",
    # але не розбити велику статтю на її внутрішні частини.
    if is_heading_style(style_name) and is_all_caps_heading(text) and len(norm) >= 4:
        return True

    return False


def split_corpus_into_articles(corpus_path: Path, codes: list[ArticleCode]) -> list[Article]:
    paragraphs = read_docx_paragraphs(corpus_path)
    known_titles = {normalize_title(c.title) for c in codes}
    code_by_title = {normalize_title(c.title): c for c in codes}

    # 1) Шукаємо реальні початки статей.
    #
    # Важливо: у DOCX є зміст, де назви статей мають стиль List Paragraph.
    # Їх НЕ можна вважати початками статей. Реальні статті в корпусі
    # оформлені стилем Heading 1 / Заголовок 1.
    #
    # Попередній алгоритм додатково перевіряв наступний абзац і вимагав, щоб
    # він мав щонайменше 6 слів. Через це випадали статті, після заголовка
    # яких іде коротке звертання або внутрішній підзаголовок, наприклад:
    # «Друзі Націоналісти-Революціонери!», «Завдання ОУН в Україні»,
    # «1. Український націоналізм і релігія». Тому кількість статей могла
    # зменшуватися до 42–43.
    #
    # Тепер критерій строгіший і надійніший: беремо тільки Heading-заголовки
    # реального тексту, а не елементи змісту.
    starts: list[int] = []
    for i, (text, style_name) in enumerate(paragraphs):
        norm = normalize_title(text)
        if not norm:
            continue
        if is_numbered_subheading(text):
            continue
        if is_heading_style(style_name) and (norm in known_titles or is_all_caps_heading(text)):
            starts.append(i)

    # 2) Дедуплікація на випадок технічних повторів заголовка в основному
    # тексті. Якщо однакова назва трапляється кілька разів як Heading,
    # залишаємо останню позицію.
    deduped: list[int] = []
    seen_positions_by_title: dict[str, int] = {}
    for idx in starts:
        norm = normalize_title(paragraphs[idx][0])
        if norm in seen_positions_by_title:
            old_idx = seen_positions_by_title[norm]
            if old_idx in deduped:
                deduped.remove(old_idx)
        seen_positions_by_title[norm] = idx
        deduped.append(idx)
    starts = sorted(deduped)

    if not starts:
        raise ValueError("Не вдалося поділити корпус на статті. Перевір, чи заголовки статей є окремими абзацами.")

    articles: list[Article] = []
    unmatched_count = 0
    for n, start_idx in enumerate(starts):
        end_idx = starts[n + 1] if n + 1 < len(starts) else len(paragraphs)
        title = clean_text_fragment(paragraphs[start_idx][0])
        norm_title = normalize_title(title)
        code_entry = code_by_title.get(norm_title)
        if code_entry:
            code = code_entry.code
        else:
            unmatched_count += 1
            code = f"БЕЗ_КОДУ_{unmatched_count:03d}"

        body_parts = [paragraphs[i][0] for i in range(start_idx + 1, end_idx)]
        text = "\n".join(body_parts).strip()
        articles.append(Article(article_id=len(articles) + 1, code=code, title=title, text=text))

    return articles


def sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for m in SENTENCE_END_RE.finditer(text):
        end = m.end()
        if end > start:
            spans.append((start, end))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    if not spans:
        spans.append((0, len(text)))
    return spans


def find_sentence_for_pos(spans: list[tuple[int, int]], pos: int) -> tuple[int, int, int]:
    # Лінійний пошук достатній для статей такого розміру; можна замінити на bisect.
    for i, (s, e) in enumerate(spans):
        if s <= pos < e:
            return i, s, e
    return max(0, len(spans) - 1), spans[-1][0], spans[-1][1]


def tokenize_article(article: Article) -> list[Token]:
    text = article.text
    spans = sentence_spans(text)
    tokens: list[Token] = []
    for idx, m in enumerate(WORD_RE.finditer(text)):
        word = m.group(0)
        norm = normalize_wordform(word)
        sent_id, sent_start, sent_end = find_sentence_for_pos(spans, m.start())
        tokens.append(Token(
            article_id=article.article_id,
            sentence_id=sent_id,
            token_index=idx,
            wordform=word,
            norm_wordform=norm,
            var_key=variant_key(norm),
            char_start=m.start(),
            char_end=m.end(),
            sentence_start=sent_start,
            sentence_end=sent_end,
        ))
    return tokens


def source_hash(paths: Iterable[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode("utf-8"))
        h.update(str(p.stat().st_mtime_ns).encode("utf-8"))
        h.update(str(p.stat().st_size).encode("utf-8"))
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()


def need_rebuild(force: bool = False) -> bool:
    if force or not DB_PATH.exists():
        return True
    try:
        corpus_path, list_path = find_source_files()
        current = source_hash([corpus_path, list_path])
        previous = HASH_PATH.read_text(encoding="utf-8").strip() if HASH_PATH.exists() else ""
        return current != previous
    except Exception:
        return True


def init_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript("""
    DROP TABLE IF EXISTS tokens;
    DROP TABLE IF EXISTS articles;
    DROP TABLE IF EXISTS wordforms;

    CREATE TABLE articles (
        article_id INTEGER PRIMARY KEY,
        code TEXT NOT NULL,
        title TEXT NOT NULL,
        text TEXT NOT NULL,
        tokens INTEGER NOT NULL
    );

    CREATE TABLE tokens (
        token_id INTEGER PRIMARY KEY AUTOINCREMENT,
        article_id INTEGER NOT NULL,
        sentence_id INTEGER NOT NULL,
        token_index INTEGER NOT NULL,
        wordform TEXT NOT NULL,
        norm_wordform TEXT NOT NULL,
        variant_key TEXT NOT NULL,
        char_start INTEGER NOT NULL,
        char_end INTEGER NOT NULL,
        sentence_start INTEGER NOT NULL,
        sentence_end INTEGER NOT NULL,
        FOREIGN KEY(article_id) REFERENCES articles(article_id)
    );

    CREATE TABLE wordforms (
        wordform TEXT PRIMARY KEY,
        frequency INTEGER NOT NULL,
        variant_key TEXT NOT NULL
    );

    CREATE INDEX idx_tokens_wordform ON tokens(norm_wordform);
    CREATE INDEX idx_tokens_wordform_article_index ON tokens(norm_wordform, article_id, token_index);
    CREATE INDEX idx_tokens_variant ON tokens(variant_key);
    CREATE INDEX idx_tokens_article ON tokens(article_id);
    CREATE INDEX idx_tokens_article_index ON tokens(article_id, token_index);
    CREATE INDEX idx_articles_code ON articles(code);
    CREATE INDEX idx_wordforms_variant ON wordforms(variant_key);
    """)
    conn.commit()


def build_index() -> None:
    ensure_dirs()
    corpus_path, list_path = find_source_files()
    codes = parse_article_codes(list_path)
    articles = split_corpus_into_articles(corpus_path, codes)

    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    cur = conn.cursor()

    all_tokens: list[Token] = []
    for article in articles:
        tokens = tokenize_article(article)
        all_tokens.extend(tokens)
        cur.execute(
            "INSERT INTO articles(article_id, code, title, text, tokens) VALUES (?, ?, ?, ?, ?)",
            (article.article_id, article.code, article.title, article.text, len(tokens)),
        )
        cur.executemany(
            """
            INSERT INTO tokens(
                article_id, sentence_id, token_index, wordform, norm_wordform, variant_key,
                char_start, char_end, sentence_start, sentence_end
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    t.article_id, t.sentence_id, t.token_index, t.wordform, t.norm_wordform, t.var_key,
                    t.char_start, t.char_end, t.sentence_start, t.sentence_end,
                )
                for t in tokens
            ],
        )

    freq = Counter(t.norm_wordform for t in all_tokens)
    cur.executemany(
        "INSERT INTO wordforms(wordform, frequency, variant_key) VALUES (?, ?, ?)",
        [(wf, fr, variant_key(wf)) for wf, fr in sorted(freq.items())],
    )
    conn.commit()
    conn.close()

    current_hash = source_hash([corpus_path, list_path])
    HASH_PATH.write_text(current_hash, encoding="utf-8")

    export_frequency_list()
    export_articles_report()
    export_variant_groups_report()

    unmatched = [a for a in articles if a.code.startswith("БЕЗ_КОДУ")]
    if unmatched:
        path = OUTPUTS_DIR / "unmatched_articles.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["code", "title"])
            for a in unmatched:
                writer.writerow([a.code, a.title])

    print(f"Статей у корпусі: {len(articles)}")
    print(f"Токенів-словоформ: {len(all_tokens)}")
    print(f"Унікальних словоформ: {len(freq)}")
    print(f"Індекс створено: {DB_PATH}")


def db_connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError("Індекс не знайдено. Спочатку запусти main.py --rebuild --mode sentence")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def export_frequency_list() -> None:
    conn = db_connect()
    rows = conn.execute(
        "SELECT wordform, frequency, variant_key FROM wordforms ORDER BY frequency DESC, wordform ASC"
    ).fetchall()
    conn.close()
    path = OUTPUTS_DIR / "frequency_list.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["wordform", "frequency", "variant_key"])
        for r in rows:
            writer.writerow([r["wordform"], r["frequency"], r["variant_key"]])


def export_articles_report() -> None:
    conn = db_connect()
    rows = conn.execute(
        "SELECT article_id, code, title, tokens FROM articles ORDER BY article_id"
    ).fetchall()
    conn.close()
    path = OUTPUTS_DIR / "articles_report.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["article_id", "code", "title", "tokens"])
        for r in rows:
            writer.writerow([r["article_id"], r["code"], r["title"], r["tokens"]])


def export_variant_groups_report() -> None:
    conn = db_connect()
    rows = conn.execute(
        "SELECT wordform, frequency, variant_key FROM wordforms ORDER BY variant_key, wordform"
    ).fetchall()
    conn.close()

    groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for r in rows:
        groups[r["variant_key"]].append((r["wordform"], int(r["frequency"])))

    path = OUTPUTS_DIR / "variant_groups_report.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        # variant_key — це внутрішній ключ групування, а variant_codes —
        # зрозумілий користувачеві код варіянтности: ІА/ІЯ, ОСТИ/ОСТІ, З/С тощо.
        writer.writerow(["variant_key", "variant_codes", "variants", "total_frequency"])
        for key, variants in sorted(groups.items()):
            if len(variants) < 2:
                continue
            total = sum(fr for _, fr in variants)
            variants_sorted = sorted(variants, key=lambda x: (-x[1], x[0]))
            variants_label = " / ".join(f"{wf} {fr}" for wf, fr in variants_sorted)
            codes_label = variant_group_codes([wf for wf, _ in variants_sorted])
            if not codes_label:
                codes_label = "інше"
            writer.writerow([key, codes_label, variants_label, total])

def get_context_sentence(article_text: str, row: sqlite3.Row) -> tuple[str, str]:
    left = clean_text_fragment(article_text[row["sentence_start"]:row["char_start"]])
    right = clean_text_fragment(article_text[row["char_end"]:row["sentence_end"]])
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

    if left_boundary:
        left = clean_text_fragment(article_text[left_boundary["char_start"]:row["char_start"]])
    else:
        left = ""
    if right_boundary:
        right = clean_text_fragment(article_text[row["char_end"]:right_boundary["char_end"]])
    else:
        right = ""
    return left or EMPTY_CONTEXT, right or EMPTY_CONTEXT


def iter_concordance_rows(mode: str, depth: int = 7) -> Iterable[dict[str, str | int]]:
    conn = db_connect()
    article_cache: dict[int, sqlite3.Row] = {}
    freq_cache = {r["wordform"]: r["frequency"] for r in conn.execute("SELECT wordform, frequency FROM wordforms")}

    rows = conn.execute(
        """
        SELECT t.*, a.code, a.title AS article_title
        FROM tokens t
        JOIN articles a ON a.article_id = t.article_id
        ORDER BY t.norm_wordform ASC, t.article_id ASC, t.token_index ASC
        """
    )

    for row in rows:
        article_id = int(row["article_id"])
        if article_id not in article_cache:
            article_cache[article_id] = conn.execute(
                "SELECT * FROM articles WHERE article_id = ?", (article_id,)
            ).fetchone()
        article = article_cache[article_id]
        article_text = article["text"]
        if mode == "sentence":
            left, right = get_context_sentence(article_text, row)
        else:
            left, right = get_context_depth(conn, article_text, row, depth)

        yield {
            "wordform": row["norm_wordform"],
            "frequency": freq_cache.get(row["norm_wordform"], 0),
            "variant_key": row["variant_key"],
            "keyword": row["wordform"],
            "left_context": left,
            "right_context": right,
            "code": row["code"],
            "article_title": row["article_title"],
        }
    conn.close()


CONCORDANCE_CSV_COLUMNS_UA = {
    "wordform": "Реєстрова словоформа",
    "frequency": "Частота словоформи",
    "variant_key": "Варіянтний ключ",
    "keyword": "Словоформа в контексті",
    "left_context": "Лівий контекст",
    "right_context": "Правий контекст",
    "code": "Код статті",
    "article_title": "Назва статті",
}


def export_concordance_csv(mode: str, depth: int = 7) -> Path:
    suffix = "sentence" if mode == "sentence" else f"depth_{depth}"
    path = OUTPUTS_DIR / f"concordance_{suffix}.csv"
    fieldnames = [
        "wordform", "frequency", "variant_key", "keyword", "left_context",
        "right_context", "code", "article_title",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            delimiter=";",
            fieldnames=[CONCORDANCE_CSV_COLUMNS_UA[col] for col in fieldnames],
        )
        writer.writeheader()
        for row in iter_concordance_rows(mode=mode, depth=depth):
            writer.writerow({CONCORDANCE_CSV_COLUMNS_UA[col]: row.get(col, "") for col in fieldnames})
    return path


def export_concordance_html(mode: str, depth: int = 7) -> Path:
    suffix = "sentence" if mode == "sentence" else f"depth_{depth}"
    path = OUTPUTS_DIR / f"concordance_{suffix}.html"
    title = "Електронний конкорданс творів-першодруків Степана Бандери"
    rows = iter_concordance_rows(mode=mode, depth=depth)

    with path.open("w", encoding="utf-8") as f:
        f.write("<!doctype html><html lang='uk'><head><meta charset='utf-8'>")
        f.write(f"<title>{html.escape(title)}</title>")
        f.write("""
        <style>
        body { font-family: Arial, sans-serif; margin: 32px; color: #1f2937; }
        h1 { font-size: 30px; }
        .headword { font-size: 23px; font-weight: 900; margin-top: 26px; border-top: 1px solid #ddd; padding-top: 14px; }
        .row { display: grid; grid-template-columns: 44% 8% 44% 4%; gap: 4px; padding: 5px 0; border-bottom: 1px solid #eee; align-items: start; }
        .left { text-align: right; font-family: 'Times New Roman', serif; font-size: 17px; }
        .kw { text-align: center; font-weight: 900; font-family: 'Times New Roman', serif; font-size: 17px; }
        .right { text-align: left; font-family: 'Times New Roman', serif; font-size: 17px; }
        .code { font-weight: 800; font-size: 13px; }
        </style></head><body>
        """)
        f.write(f"<h1>{html.escape(title)}</h1>")
        current = None
        for row in rows:
            if row["wordform"] != current:
                current = row["wordform"]
                f.write(f"<div class='headword'>{html.escape(str(row['wordform']))} {row['frequency']}</div>")
            f.write("<div class='row'>")
            f.write(f"<div class='left'>{html.escape(str(row['left_context']))}</div>")
            f.write(f"<div class='kw'>{html.escape(str(row['keyword']))}</div>")
            f.write(f"<div class='right'>{html.escape(str(row['right_context']))}</div>")
            f.write(f"<div class='code'>{html.escape(str(row['code']))}</div>")
            f.write("</div>")
        f.write("</body></html>")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Побудова конкордансу творів Степана Бандери")
    parser.add_argument("--rebuild", action="store_true", help="примусово перебудувати індекс")
    parser.add_argument("--mode", choices=["sentence", "depth"], default="sentence", help="режим експорту конкордансу")
    parser.add_argument("--depth", type=int, default=7, help="глибина контексту для режиму depth")
    parser.add_argument("--no-export", action="store_true", help="побудувати індекс без експорту повного конкордансу")
    args = parser.parse_args()

    ensure_dirs()
    if need_rebuild(args.rebuild):
        build_index()
    else:
        print("Індекс уже актуальний. Перебудова не потрібна.")
        # На випадок, якщо звіти видалені вручну.
        export_frequency_list()
        export_articles_report()
        export_variant_groups_report()

    if not args.no_export:
        csv_path = export_concordance_csv(args.mode, args.depth)
        html_path = export_concordance_html(args.mode, args.depth)
        print(f"CSV створено: {csv_path}")
        print(f"HTML створено: {html_path}")


if __name__ == "__main__":
    main()
