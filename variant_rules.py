# -*- coding: utf-8 -*-
"""
Правила варіянтного об'єднання словоформ для електронного конкордансу
творів-першодруків Степана Бандери.

Цей модуль НЕ змінює текст у контекстах. Він створює лише спільний
пошуковий / реєстровий ключ. Якщо дві реально засвідчені словоформи після
застосування правил мають однаковий ключ, у конкордансі їх можна подати
як одну варіянтну реєстрову групу з окремими частотами та сумарною частотою.

Важливі обмеження:
1. Відповідність І / И НЕ застосовується до кінцевих флексій, щоб не
   об'єднувати різні словоформи на зразок УКРАЇНИ / УКРАЇНІ.
2. Відповідність С / З застосовується тільки всередині слова, щоб не
   об'єднувати різні словоформи на зразок СВІТІ / ЗВІТИ.
3. Правила НАРОДНІЙ / НАРОДНИЙ, НАРОДНЬОГО / НАРОДНОГО тощо вилучено:
   такі форми не групуються автоматично.
4. Для звіту варіянтних груп модуль уміє повертати не тільки внутрішній
   ключ групування, а й коди варіянтности: ІА/ІЯ, ОСТИ/ОСТІ, З/С тощо.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

APOSTROPHE_TRANSLATION = str.maketrans({
    "'": "’",
    "`": "’",
    "ʼ": "’",
    "ʹ": "’",
    "‘": "’",
    "՚": "’",
})

HYPHEN_TRANSLATION = str.maketrans({
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "−": "-",
})

# Точні відповідники: вони не завжди зводяться до простого фонетико-правописного
# правила, тому краще задати їх вручну. Канонічний відповідник теж залишено в
# EXACT_EQUIVALENTS, щоб пошук за будь-якою формою потрапляв у ту саму групу.
EXACT_EQUIVALENTS = {
    "МОЙОГО": "МОГО",
    "МОГО": "МОГО",

    "СВОЙОМУ": "СВОЄМУ",
    "СВОЄМУ": "СВОЄМУ",

    "СВОЙОГО": "СВОГО",
    "СВОГО": "СВОГО",

    "ТВОЙОГО": "ТВОГО",
    "ТВОГО": "ТВОГО",

    "ТОЇ": "ТІЄЇ",
    "ТІЄЇ": "ТІЄЇ",

    "ТОЮ": "ТІЄЮ",
    "ТІЄЮ": "ТІЄЮ",

    "ЦЕЮ": "ЦІЄЮ",
    "ЦІЄЮ": "ЦІЄЮ",

    "УСІМИ": "УСІМА",
    "УСІМА": "УСІМА",

    "ВСІМИ": "ВСІМА",
    "ВСІМА": "ВСІМА",
}

# Для коду варіянтности точні правила позначаємо тільки на НЕканонічній формі.
# Канонічна форма не отримує коду, інакше у звіті не було б видно, яке саме
# розходження створило групу.
EXACT_VARIANT_CODES = {
    "МОЙОГО": "МОЙОГО/МОГО",
    "СВОЙОМУ": "СВОЙОМУ/СВОЄМУ",
    "СВОЙОГО": "СВОЙОГО/СВОГО",
    "ТВОЙОГО": "ТВОЙОГО/ТВОГО",
    "ТОЇ": "ТОЇ/ТІЄЇ",
    "ТОЮ": "ТОЮ/ТІЄЮ",
    "ЦЕЮ": "ЦЕЮ/ЦІЄЮ",
    "УСІМИ": "УСІМИ/УСІМА",
    "ВСІМИ": "ВСІМИ/ВСІМА",
}

# Загальні правописно-фонетичні відповідності.
# Довші відповідності ставимо перед коротшими.
# І/И та С/З винесено в окремі функції, бо ці правила потребують обмежень.
EQUIVALENCE_REPLACEMENTS = [
    # 12) льо / ло: дипльом — диплом
    ("ЛЬО", "ЛО", "ЛЬО/ЛО"),

    # 11) лє / ле: лєкція — лекція
    ("ЛЄ", "ЛЕ", "ЛЄ/ЛЕ"),

    # 9) ля / ла: балянс — баланс, плян — план
    ("ЛЯ", "ЛА", "ЛЯ/ЛА"),

    # 10) ль / л: алькоголь — алкоголь
    ("ЛЬ", "Л", "ЛЬ/Л"),

    # 14) ія / іа: ініціятива — ініціатива, матеріял — матеріал
    ("ІЯ", "ІА", "ІА/ІЯ"),

    # 15) ію / іу: тріюмф — тріумф
    ("ІЮ", "ІУ", "ІУ/ІЮ"),

    # 5) йов / єв: бойовий — боєвий, дійовий — дієвий
    ("ЙОВ", "ЄВ", "ЙОВ/ЄВ"),

    # 8) г / ґ: еміґрація — еміграція, ґрупа — група
    ("Ґ", "Г", "Ґ/Г"),

    # 13) е / є: Европа — Європа, східноевропейський — східноєвропейський
    ("Е", "Є", "Е/Є"),
]

# Кінцеві відповідності. Вони застосовуються тільки в кінці словоформи.
SUFFIX_REPLACEMENTS = [
    # 4) -ости / -ості: дійсности — дійсності
    ("ОСТИ", "ОСТІ", "ОСТИ/ОСТІ"),

    # 6) розвиткові — розвитку; діячеві — діячу; краєві — краю
    ("ОВІ", "У", "ОВІ/У"),
    ("ЕВІ", "Ю", "ЕВІ/Ю"),
    ("ЄВІ", "Ю", "ЄВІ/Ю"),
]

# 18) аналіза — аналіз, діягноза — діягноз.
# Не відкидаємо будь-яке фінальне -А, а лише типові книжні варіянти.
FINAL_A_PATTERNS = [
    (re.compile(r"(.*ЛІЗ)А$"), "А/∅"),
    (re.compile(r"(.*ГНОЗ)А$"), "А/∅"),
    (re.compile(r"(.*НОЗ)А$"), "А/∅"),
]

# Щоб широкі правила не зачіпали дуже короткі службові одиниці.
MIN_LENGTH_FOR_BROAD_RULES = 4

# Для правила І / И: ці фінальні зони слова НЕ нормалізуємо.
# Це захищає різні словоформи типу УКРАЇНИ / УКРАЇНІ,
# АНТИБОЛЬШЕВИЦЬКИЙ / АНТИБОЛЬШЕВИЦЬКІЙ, НАРОДНІМ / НАРОДНИМ.
PROTECTED_I_Y_ENDINGS = (
    "І",      # УКРАЇНІ не зводимо до УКРАЇНИ
    "ІЙ",     # АНТИБОЛЬШЕВИЦЬКІЙ не зводимо до АНТИБОЛЬШЕВИЦЬКИЙ
    "НІМ",    # НАРОДНІМ не зводимо до НАРОДНИМ
    "НІХ",    # НАРОДНІХ не зводимо до НАРОДНИХ
    "НІМИ",   # НАРОДНІМИ не зводимо до НАРОДНИМИ
)


def normalize_wordform(word: str) -> str:
    """Технічна нормалізація словоформи для пошуку й групування."""
    if word is None:
        return ""
    word = str(word).strip()
    word = word.translate(APOSTROPHE_TRANSLATION)
    word = word.translate(HYPHEN_TRANSLATION)
    word = word.upper()
    return word


def _replace_all_with_codes(word: str, collect_codes: bool = False) -> tuple[str, set[str]]:
    codes: set[str] = set()
    w = word
    for old, new, code in EQUIVALENCE_REPLACEMENTS:
        if old in w:
            w = w.replace(old, new)
            if collect_codes:
                codes.add(code)
    return w, codes


def _replace_s_z_inside_with_code(word: str, collect_codes: bool = False) -> tuple[str, set[str]]:
    """
    Нормалізує С / З тільки всередині слова.

    Об'єднує:
        ДИВЕРСІЯ / ДИВЕРЗІЯ

    Не об'єднує:
        СВІТІ / ЗВІТИ

    Тобто перша й остання літера не змінюються.
    """
    if len(word) < 3:
        return word, set()
    chars = list(word)
    changed = False
    for i in range(1, len(chars) - 1):
        if chars[i] == "С":
            chars[i] = "З"
            changed = True
    codes = {"З/С"} if changed and collect_codes else set()
    return "".join(chars), codes


def apply_suffix_rules(word: str, collect_codes: bool = False) -> tuple[str, set[str]]:
    for old, new, code in SUFFIX_REPLACEMENTS:
        if word.endswith(old):
            stem = word[:-len(old)]
            # Правило ОВІ/У, ЕВІ/Ю, ЄВІ/Ю стосується переважно давального
            # відмінка іменникових форм на зразок РОЗВИТКОВІ / РОЗВИТКУ,
            # ДІЯЧЕВІ / ДІЯЧУ. Не застосовуємо його до дуже коротких основ,
            # бо інакше ДІЄВІ помилково зводиться до ДІЮ.
            if code in {"ОВІ/У", "ЕВІ/Ю", "ЄВІ/Ю"} and len(stem) < 4:
                continue
            return stem + new, ({code} if collect_codes else set())
    return word, set()


def apply_final_a_rules(word: str, collect_codes: bool = False) -> tuple[str, set[str]]:
    for pattern, code in FINAL_A_PATTERNS:
        match = pattern.match(word)
        if match:
            return match.group(1), ({code} if collect_codes else set())
    return word, set()


def apply_i_y_rule(word: str, collect_codes: bool = False) -> tuple[str, set[str]]:
    """
    Нормалізує І / И тільки там, де ця різниця не є кінцевою флексією.

    Об'єднує:
        РЕЖІМ / РЕЖИМ

    Не об'єднує:
        УКРАЇНІ / УКРАЇНИ
        АНТИБОЛЬШЕВИЦЬКІЙ / АНТИБОЛЬШЕВИЦЬКИЙ
        НАРОДНІМ / НАРОДНИМ
    """
    for ending in PROTECTED_I_Y_ENDINGS:
        if word.endswith(ending):
            stem = word[:-len(ending)]
            new_stem = stem.replace("І", "И")
            changed = new_stem != stem
            return new_stem + ending, ({"І/И"} if changed and collect_codes else set())
    new_word = word.replace("І", "И")
    changed = new_word != word
    return new_word, ({"І/И"} if changed and collect_codes else set())


def variant_key_and_word_codes(word: str) -> tuple[str, set[str]]:
    """
    Повертає внутрішній ключ групування та технічні коди правил,
    які були застосовані саме до цієї словоформи.

    Увага: для звіту варіянтних груп краще використовувати variant_group_codes(),
    бо вона відкидає коди, спільні для всіх словоформ групи й тому не релевантні
    для пояснення різниці між варіянтами.
    """
    w = normalize_wordform(word)
    if not w:
        return w, set()

    if w in EXACT_EQUIVALENTS:
        key = EXACT_EQUIVALENTS[w]
        code = EXACT_VARIANT_CODES.get(w)
        return key, ({code} if code else set())

    codes: set[str] = set()

    w, c = apply_suffix_rules(w, collect_codes=True)
    codes |= c

    w, c = apply_final_a_rules(w, collect_codes=True)
    codes |= c

    if len(w) < MIN_LENGTH_FOR_BROAD_RULES:
        return w, codes

    w, c = _replace_all_with_codes(w, collect_codes=True)
    codes |= c

    # С/З — тільки всередині слова, не на початку й не в кінці.
    w, c = _replace_s_z_inside_with_code(w, collect_codes=True)
    codes |= c

    w, c = apply_i_y_rule(w, collect_codes=True)
    codes |= c

    return w, codes


def variant_key(word: str) -> str:
    """Повертає внутрішній ключ варіянтної групи."""
    key, _ = variant_key_and_word_codes(word)
    return key


def _pairwise_visible_codes(a: str, b: str) -> set[str]:
    """Коди, які видно саме з різниці між двома словоформами."""
    a = normalize_wordform(a)
    b = normalize_wordform(b)
    codes: set[str] = set()
    if not a or not b or a == b:
        return codes

    if len(a) == len(b):
        pair_map = {
            frozenset(("Е", "Є")): "Е/Є",
            frozenset(("З", "С")): "З/С",
            frozenset(("Г", "Ґ")): "Ґ/Г",
            frozenset(("І", "И")): "І/И",
        }
        diff_pairs = [frozenset((x, y)) for x, y in zip(a, b) if x != y]
        if diff_pairs:
            for pair, code in pair_map.items():
                if any(d == pair for d in diff_pairs):
                    codes.add(code)
            # Якщо всі відмінності належать до відомих односимвольних пар,
            # не додаємо зайвого "інше".
            if all(d in pair_map for d in diff_pairs):
                return codes

    # Додатково перевіряємо найтиповіші багато літерні відповідності.
    known_patterns = [
        ("ОСТИ", "ОСТІ", "ОСТИ/ОСТІ"),
        ("ЛЬО", "ЛО", "ЛЬО/ЛО"),
        ("ЛЄ", "ЛЕ", "ЛЄ/ЛЕ"),
        ("ЛЯ", "ЛА", "ЛЯ/ЛА"),
        ("ЛЬ", "Л", "ЛЬ/Л"),
        ("ІЯ", "ІА", "ІА/ІЯ"),
        ("ІЮ", "ІУ", "ІУ/ІЮ"),
        ("ЙОВ", "ЄВ", "ЙОВ/ЄВ"),
        ("ОВІ", "У", "ОВІ/У"),
        ("ЕВІ", "Ю", "ЕВІ/Ю"),
        ("ЄВІ", "Ю", "ЄВІ/Ю"),
    ]
    for x, y, code in known_patterns:
        ax = a.replace(x, y)
        bx = b.replace(x, y)
        ay = a.replace(y, x)
        by = b.replace(y, x)
        if ax == b or bx == a or ax == bx or ay == b or by == a or ay == by:
            codes.add(code)

    return codes


def variant_group_codes(wordforms: Iterable[str]) -> str:
    """
    Повертає коди варіянтности для групи словоформ.

    Уточнення: код визначаємо не лише за тим, які правила спрацювали під час
    нормалізації, а й за реальною видимою різницею між словоформами. Тому
    ЕВГЕН / ЄВГЕН отримує Е/Є, а ЕКСПАНСІЄЮ / ЕКСПАНЗІЄЮ — З/С.
    """
    normalized = [normalize_wordform(w) for w in wordforms if normalize_wordform(w)]
    if not normalized:
        return ""

    relevant: set[str] = set()
    for i, a in enumerate(normalized):
        for b in normalized[i + 1:]:
            relevant |= _pairwise_visible_codes(a, b)

    # Резервний механізм: якщо видима різниця складніша, беремо технічні коди,
    # але відкидаємо ті, що спрацювали для всіх словоформ групи.
    per_word_codes: list[set[str]] = []
    for w in normalized:
        _, codes = variant_key_and_word_codes(w)
        per_word_codes.append(codes)
    counts = Counter(code for codes in per_word_codes for code in codes)
    n = len(per_word_codes)
    relevant |= {code for code, count in counts.items() if 0 < count < n}

    return "; ".join(sorted(relevant))


# Сумісність із можливими старими назвами функцій.
canonical_variant_key = variant_key
make_variant_key = variant_key
normalize_variant_key = variant_key
