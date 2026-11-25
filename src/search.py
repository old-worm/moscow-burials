"""Поисковый модуль по базе захоронений SQLite + FTS5 (unicode61, fts5vocab, trigram)."""

from __future__ import annotations

from dataclasses import dataclass
import datetime
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from src.db import open_search_db
from src.models import (
    PersonFilter,
    PersonResult,
    PlotResult,
    SearchQuery,
    SearchResponse,
)
from src.parser import (
    build_epoisk_map_url,
    build_extra_map_urls,
    build_urls,
    format_grave_type,
    normalize_text,
    split_display_inscription,
)

RUS_ALPHABET = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
LAT_ALPHABET = "abcdefghijklmnopqrstuvwxyz"

# Выделение токенов в кавычках или отдельных слов вне кавычек
QUERY_SEGMENT_RE = re.compile(r'"([^"]+)"|(\S+)')
# Разделение слитных инициалов вида "е.я." -> "е. я."
GLUED_INITIALS_RE = re.compile(r"([а-яa-z]\.)(?=[а-яa-z])")
TOKEN_ALLOWED_RE = re.compile(r"[^а-яa-z0-9|*?~$.\-]+")
LITERAL_CHUNKS_RE = re.compile(r"[*?]+")
YEAR_ONLY_RE = re.compile(r"^\d{4}$")

SURNAME_ADJ_FLEXIONS = ("ий", "ый", "ой", "ая", "яя")
SURNAME_FEM_POSSESSIVE_FLEXIONS = ("ова", "ева", "ина", "ына")

MALE_NAMES_ENDING_IY = frozenset(
    {
        "анатолий",
        "аркадий",
        "арсений",
        "артемий",
        "афанасий",
        "василий",
        "валерий",
        "виталий",
        "геннадий",
        "георгий",
        "григорий",
        "дмитрий",
        "евгений",
        "евстафий",
        "зиновий",
        "игнатий",
        "иннокентий",
        "клементий",
        "корнелий",
        "лаврентий",
        "леонтий",
        "макарий",
        "мефодий",
        "порфирий",
        "прокофий",
        "савелий",
        "терентий",
        "феодосий",
        "юлий",
        "юрий",
    }
)

FEMALE_NAMES_ENDING_INA = frozenset(
    {
        "аделина",
        "алина",
        "альбина",
        "ангелина",
        "антонина",
        "валентина",
        "галина",
        "дина",
        "екатерина",
        "зина",
        "ирина",
        "карина",
        "кристина",
        "марина",
        "нина",
        "полина",
        "регина",
        "фаина",
        "эвелина",
    }
)


class _NoMatchError(Exception):
    """Внутреннее исключение, сигнализирующее о заведомо пустом множестве совпадений."""


@dataclass
class _ParsedAlt:
    """Разобранная альтернатива поискового токена ФИО.

    Attributes:
        text: Очищенный текст шаблона слова (без кавычек, концевой точки и '$').
        exact_word: Точная словоформа (из кавычек "..." или инициала с точкой 'е.').
        exact_end: Запрет подстановки произвольного окончания (из '$', кавычек или точки).
    """

    text: str
    exact_word: bool
    exact_end: bool


@dataclass
class _DateBound:
    """Разобранная граница даты (год, месяц+год или полная дата ДД.ММ.ГГГГ).

    Attributes:
        year: Четырёхзначный год (1000–2999).
        month: Номер месяца (1–12) или None, если указан только год.
        day: День месяца (1–31) или None, если указан только год или месяц+год.
    """

    year: int
    month: int | None = None
    day: int | None = None

    def encode(self) -> str:
        """Кодирует границу даты в компактную строку 'YYYY:MM:DD' для SQLite-функции."""
        return f"{self.year}:{self.month or 0}:{self.day or 0}"


@dataclass
class _DateSpec:
    """Разобранное поисковое выражение даты (одиночное значение или диапазон).

    Attributes:
        is_range: Флаг диапазона (через дефис/тире).
        lower: Нижняя граница (для одиночного значения совпадает с upper).
        upper: Верхняя граница (для одиночного значения совпадает с lower).
    """

    is_range: bool
    lower: _DateBound | None
    upper: _DateBound | None

    def encode(self) -> str:
        """Кодирует выражение даты в строку для передачи в SQLite-функцию match_date_spec."""
        mode = "R" if self.is_range else "E"
        lo_s = self.lower.encode() if self.lower is not None else ""
        hi_s = self.upper.encode() if self.upper is not None else ""
        return f"{mode}|{lo_s}|{hi_s}"


_MONTH_YEAR_RE = re.compile(r"^(\d{1,2})\.(\d{4})$")
_DAY_MONTH_YEAR_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")


def _parse_single_date_bound(raw_bound: str) -> _DateBound:
    """Разбирает и валидирует одну границу даты (ГГГГ, ММ.ГГГГ или ДД.ММ.ГГГГ).

    Args:
        raw_bound: Строка одной даты или границы диапазона.

    Returns:
        Объект _DateBound.

    Raises:
        ValueError: При нарушении формата или несуществующей календарной дате.
    """
    s = raw_bound.strip()
    if YEAR_ONLY_RE.match(s):
        year = int(s)
        if not (1000 <= year <= 2999):
            raise ValueError(f"Некорректный год: {s!r}")
        return _DateBound(year=year)

    m_my = _MONTH_YEAR_RE.match(s)
    if m_my:
        month = int(m_my.group(1))
        year = int(m_my.group(2))
        if not (1000 <= year <= 2999):
            raise ValueError(f"Некорректный год в дате: {s!r}")
        if not (1 <= month <= 12):
            raise ValueError(f"Некорректный месяц в дате {s!r} (допустимо 01–12)")
        return _DateBound(year=year, month=month)

    m_dmy = _DAY_MONTH_YEAR_RE.match(s)
    if m_dmy:
        day = int(m_dmy.group(1))
        month = int(m_dmy.group(2))
        year = int(m_dmy.group(3))
        if not (1000 <= year <= 2999):
            raise ValueError(f"Некорректный год в дате: {s!r}")
        if not (1 <= month <= 12):
            raise ValueError(f"Некорректный месяц в дате {s!r} (допустимо 01–12)")
        try:
            datetime.date(year, month, day)
        except ValueError as exc:
            raise ValueError(f"Несуществующая календарная дата: {s!r}") from exc
        return _DateBound(year=year, month=month, day=day)

    raise ValueError(
        f"Неверный формат даты {s!r}. Используйте ГГГГ, ММ.ГГГГ, ДД.ММ.ГГГГ "
        "или диапазон через дефис (например: 1900-12.12.1967)"
    )


def parse_date_filter_expr(raw_expr: str | None) -> _DateSpec | None:
    """Разбирает и валидирует поисковое выражение даты или диапазона дат.

    Поддерживает:
      - одиночные значения: '1967', '02.1967', '12.12.1967';
      - закрытые диапазоны: '1900-1967', '1900-12.12.1967', '02.1967-10.1967', '01.05.1900-12.12.1967';
      - открытые диапазоны: '1900-', '02.1967-', '-1967', '-12.12.1967'.

    Args:
        raw_expr: Сырая строка из поля фильтра даты.

    Returns:
        Объект _DateSpec или None, если строка пуста.

    Raises:
        ValueError: При неверном формате, несуществующей дате или начале диапазона позже конца.
    """
    if raw_expr is None:
        return None
    cleaned = re.sub(r"[–—]|\.\.", "-", str(raw_expr)).strip()
    if not cleaned:
        return None

    if "-" not in cleaned:
        bound = _parse_single_date_bound(cleaned)
        return _DateSpec(is_range=False, lower=bound, upper=bound)

    parts = [p.strip() for p in cleaned.split("-")]
    if len(parts) > 2:
        raise ValueError(
            f"Неверный формат диапазона дат {raw_expr!r}. Разделитель дней и месяцев — точка, "
            "диапазона — один дефис (например: 1900-12.12.1967)"
        )

    left_str, right_str = parts[0], parts[1]
    if not left_str and not right_str:
        raise ValueError("Не указаны границы диапазона дат")

    lower = _parse_single_date_bound(left_str) if left_str else None
    upper = _parse_single_date_bound(right_str) if right_str else None

    if lower is not None and upper is not None:
        lo_min = (lower.year, lower.month or 1, lower.day or 1)
        hi_max = (upper.year, upper.month or 12, upper.day or 31)
        if lo_min > hi_max:
            raise ValueError(
                f"Начало диапазона дат ({left_str}) позже конца диапазона ({right_str})"
            )

    return _DateSpec(is_range=True, lower=lower, upper=upper)


def _extract_record_ymd(
    raw_date: str | None, year_col: int | None
) -> tuple[int | None, int | None, int | None]:
    """Извлекает (год, месяц, день) из сырой даты памятника ('DD.MM.YYYY', 'год YYYY', 'месяц MM,год YYYY').

    Args:
        raw_date: Сырая строка даты из базы данных.
        year_col: Числовой год из колонки birth_year / death_year / single_date_year.

    Returns:
        Кортеж (year, month, day), где неизвестные компоненты равны None.
    """
    if not raw_date:
        return (year_col, None, None)

    if len(raw_date) == 10 and raw_date[2] == "." and raw_date[5] == ".":
        d_s, m_s, y_s = raw_date[0:2], raw_date[3:5], raw_date[6:10]
        d = int(d_s) if d_s.isdigit() else 0
        m = int(m_s) if m_s.isdigit() else 0
        y = int(y_s) if y_s.isdigit() else (year_col or 0)
        return (
            y if y > 0 else year_col,
            m if 1 <= m <= 12 else None,
            d if 1 <= d <= 31 else None,
        )

    m_val: int | None = None
    d_val: int | None = None
    for part in raw_date.split(","):
        p = part.strip()
        if p.startswith("месяц "):
            num_s = p[6:].strip()
            if num_s.isdigit():
                v = int(num_s)
                if 1 <= v <= 12:
                    m_val = v
        elif p.startswith("день "):
            num_s = p[5:].strip()
            if num_s.isdigit():
                v = int(num_s)
                if 1 <= v <= 31:
                    d_val = v
    return (year_col, m_val, d_val)


def _decode_bound_triple(encoded: str) -> tuple[int, int | None, int | None] | None:
    """Декодирует строку 'YYYY:MM:DD' обратно в кортеж (year, month, day).

    Args:
        encoded: Закодированная граница даты вида 'YYYY:MM:DD'.

    Returns:
        Кортеж (year, month, day) или None, если строка пуста.
    """
    if not encoded:
        return None
    y_s, m_s, d_s = encoded.split(":")
    y = int(y_s)
    m = int(m_s)
    d = int(d_s)
    return (y, m if m > 0 else None, d if d > 0 else None)


def _sqlite_match_date_spec(
    raw_date: str | None, year_col: int | None, encoded_spec: str
) -> int:
    """Пользовательская SQLite-функция проверки соответствия даты памятника шаблону _DateSpec.

    При поиске по диапазону (режим 'R') записи, у которых на памятнике известен только год
    (или только год и месяц), включаются в результаты, если известные компоненты попадают в диапазон.

    Args:
        raw_date: Сырая строка даты из таблицы persons.
        year_col: Числовой год из таблицы persons.
        encoded_spec: Закодированное представление _DateSpec ('E|...' или 'R|...').

    Returns:
        1 при совпадении даты с шаблоном, иначе 0.
    """
    rec_y, rec_m, rec_d = _extract_record_ymd(raw_date, year_col)
    if rec_y is None:
        return 0

    mode, lo_enc, hi_enc = encoded_spec.split("|")
    lo = _decode_bound_triple(lo_enc)
    hi = _decode_bound_triple(hi_enc)

    if mode == "E":
        if lo is None:
            return 0
        b_y, b_m, b_d = lo
        if rec_y != b_y:
            return 0
        if b_m is not None and rec_m != b_m:
            return 0
        if b_d is not None and rec_d != b_d:
            return 0
        return 1

    # Режим 'R' (диапазон): известные компоненты не должны противоречить границам
    if lo is not None:
        lo_y, lo_m, lo_d = lo
        if rec_y < lo_y:
            return 0
        if rec_y == lo_y and lo_m is not None and rec_m is not None:
            if rec_m < lo_m:
                return 0
            if rec_m == lo_m and lo_d is not None and rec_d is not None:
                if rec_d < lo_d:
                    return 0

    if hi is not None:
        hi_y, hi_m, hi_d = hi
        if rec_y > hi_y:
            return 0
        if rec_y == hi_y and hi_m is not None and rec_m is not None:
            if rec_m > hi_m:
                return 0
            if rec_m == hi_m and hi_d is not None and rec_d is not None:
                if rec_d > hi_d:
                    return 0

    return 1


def _alphabet_for_word(word: str) -> str:
    """Выбирает алфавит для перебора первой буквы в шаблонах вида '?околов'.

    Args:
        word: Часть шаблона после ведущего символа '?'.

    Returns:
        Строка символов алфавита (русский, латинский или оба).
    """
    has_lat = any("a" <= ch <= "z" for ch in word)
    has_rus = any("а" <= ch <= "я" for ch in word)
    if has_rus and not has_lat:
        return RUS_ALPHABET
    if has_lat and not has_rus:
        return LAT_ALPHABET
    return RUS_ALPHABET + LAT_ALPHABET


def _strip_flexion(word: str) -> str:
    """Отсекает родовое окончание фамилии в именительном падеже для поиска по основе.

    Сводит мужскую и женскую форму фамилии к общей основе:
      - прилагательные ('-ий', '-ый', '-ой', '-ая', '-яя'):
        'Красновский' / 'Красновская' -> 'красновск'
      - женские притяжательные ('-ова', '-ева', '-ина', '-ына'):
        'Соколова' -> 'соколов', 'Щукина' -> 'щукин'
    Личные мужские имена на '-ий' ('Евгений', 'Дмитрий') и женские на '-ина'
    ('Галина', 'Марина', 'Ирина') не урезаются.

    Args:
        word: Слово в нижнем регистре (ё -> е).

    Returns:
        Основа слова без родового окончания фамилии.
    """
    if len(word) < 4 or not word.isalpha():
        return word

    if word not in MALE_NAMES_ENDING_IY and word.endswith(SURNAME_ADJ_FLEXIONS):
        stem = word[:-2]
        if len(stem) >= 3:
            return stem

    if word not in FEMALE_NAMES_ENDING_INA and word.endswith(
        SURNAME_FEM_POSSESSIVE_FLEXIONS
    ):
        stem = word[:-1]
        if len(stem) >= 3:
            return stem

    return word


def _expand_tilde_patterns(text: str) -> list[str]:
    """Раскрывает оператор '~' (0 или 1 любой символ) в набор шаблонов с '?'.

    Например:
      - 'иван~' -> ['иван', 'иван?']
      - 'куз~миченков' -> ['кузмиченков', 'куз?миченков']

    Args:
        text: Шаблон слова, возможно содержащий '~'.

    Returns:
        Список шаблонов без '~' (где на месте '~' либо пусто, либо '?').
    """
    if "~" not in text:
        return [text]

    parts = text.split("~")
    results = [parts[0]]
    for part in parts[1:]:
        if len(results) < 16:
            next_res: list[str] = []
            for prefix in results:
                next_res.append(prefix + part)
                next_res.append(prefix + "?" + part)
            results = next_res
        else:
            results = [prefix + "?" + part for prefix in results]

    seen: set[str] = set()
    unique: list[str] = []
    for r in results:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def _next_prefix_bound(prefix: str) -> str:
    """Вычисляет верхнюю границу диапазона строк для префиксного поиска в B-Tree.

    Args:
        prefix: Непустая строка-префикс.

    Returns:
        Строка верхней границы диапазона (exclusively).
    """
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _lookup_wildcard_vocab_terms(
    conn: sqlite3.Connection, glob_pattern: str
) -> list[str]:
    """Находит в persons_vocab лексемы, соответствующие GLOB-шаблону со звёздочками/вопросами.

    Использует быстрый индексный скан по префиксу, если шаблон начинается с литералов.

    Args:
        conn: Открытое соединение SQLite.
        glob_pattern: Шаблон GLOB (например, 'куз?миченков*' или 'иван?').

    Returns:
        Список кавыченных термов для подстановки в FTS5.
    """
    m = re.match(r"^([а-яa-z0-9]+)", glob_pattern)
    if m:
        prefix = m.group(1)
        upper = _next_prefix_bound(prefix)
        rows = conn.execute(
            "SELECT term FROM persons_vocab "
            "WHERE term >= ? AND term < ? AND term GLOB ? LIMIT 500",
            (prefix, upper, glob_pattern),
        ).fetchall()
    elif glob_pattern.startswith("?") and len(glob_pattern) >= 2:
        rest = glob_pattern[1:]
        m_rest = re.match(r"^([а-яa-z0-9]+)", rest)
        if m_rest:
            lit = m_rest.group(1)
            alphabet = _alphabet_for_word(lit)
            terms: list[str] = []
            for ch in alphabet:
                p = ch + lit
                u = _next_prefix_bound(p)
                rows = conn.execute(
                    "SELECT term FROM persons_vocab "
                    "WHERE term >= ? AND term < ? AND term GLOB ? LIMIT 200",
                    (p, u, glob_pattern),
                ).fetchall()
                terms.extend(f'"{r[0]}"' for r in rows)
                if len(terms) >= 500:
                    break
            return terms[:500]
        rows = conn.execute(
            "SELECT term FROM persons_vocab WHERE term GLOB ? LIMIT 500",
            (glob_pattern,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT term FROM persons_vocab WHERE term GLOB ? LIMIT 500",
            (glob_pattern,),
        ).fetchall()

    return [f'"{r[0]}"' for r in rows]


def _escape_fts_token(token: str) -> str:
    """Экранирует двойные кавычки внутри токена FTS5.

    Args:
        token: Исходная строка токена.

    Returns:
        Экранированная строка токена.
    """
    return token.replace('"', '""')


def _parse_single_alt(raw_alt: str, quoted: bool) -> _ParsedAlt | None:
    """Разбирает одну альтернативу слова с учётом кавычек, точки и символа '$'.

    Args:
        raw_alt: Сырая строка альтернативы.
        quoted: Было ли слово заключено в кавычки.

    Returns:
        Экземпляр _ParsedAlt или None, если токен пуст после очистки.
    """
    s = TOKEN_ALLOWED_RE.sub("", raw_alt).strip()
    if not s:
        return None

    exact_word = quoted
    exact_end = quoted

    if s.endswith("."):
        s = s.rstrip(".")
        exact_word = True
        exact_end = True

    if s.endswith("$"):
        s = s.rstrip("$")
        exact_end = True

    s = s.strip(".")
    if not s:
        return None

    return _ParsedAlt(text=s, exact_word=exact_word, exact_end=exact_end)


def _tokenize_fio_query(fio: str) -> list[list[_ParsedAlt]]:
    """Разбивает поисковую строку ФИО на позиции слов и список альтернатив на каждой позиции.

    Поддерживает:
      - кавычки ("Красновский") — точная словоформа без отсечения окончаний;
      - точки после инициалов (Е. Я. или Е.Я.) — точные инициалы;
      - символ '$' в конце слова (Иван$) — точный конец слова;
      - оператор '|' — альтернативы внутри одной позиции слова.

    Args:
        fio: Сырая строка ФИО из фильтра.

    Returns:
        Список позиций, где каждая позиция — непустой список объектов _ParsedAlt.
    """
    if not fio or not fio.strip():
        return []

    text = fio.lower().replace("ё", "е")
    text = re.sub(r"[«»„“”']", '"', text)
    text = re.sub(r"[,();:]+", " ", text)

    positions: list[list[_ParsedAlt]] = []
    for match in QUERY_SEGMENT_RE.finditer(text):
        quoted_group, unquoted_group = match.group(1), match.group(2)
        if quoted_group is not None:
            for subword in quoted_group.split():
                alt = _parse_single_alt(subword, quoted=True)
                if alt is not None:
                    positions.append([alt])
        elif unquoted_group is not None:
            expanded = GLUED_INITIALS_RE.sub(r"\1 ", unquoted_group)
            for token_str in expanded.split():
                raw_alts = [a for a in token_str.split("|") if a]
                parsed_alts: list[_ParsedAlt] = []
                for ra in raw_alts:
                    pa = _parse_single_alt(ra, quoted=False)
                    if pa is not None:
                        parsed_alts.append(pa)
                if parsed_alts:
                    positions.append(parsed_alts)

    return positions


def _build_prefix_fts_clause_for_alt(
    conn: sqlite3.Connection,
    alt: _ParsedAlt,
    col_spec: str,
    glob_cols: tuple[str, ...],
    extra_globs: list[tuple[tuple[str, ...], str]],
    trigram_accumulator: list[str],
) -> str | None:
    """Строит FTS5-выражение для одной альтернативы в режимах prefix_default / prefix_no_surname.

    Args:
        conn: Открытое соединение SQLite.
        alt: Разобранная альтернатива поискового токена.
        col_spec: Имя колонки или список колонок FTS5 (например, 'surname' или '{firstname patronymic}').
        glob_cols: Кортеж имён колонок таблицы persons для GLOB-фильтрации.
        extra_globs: Выходной список дополнительных условий GLOB при ведущей '*'.
        trigram_accumulator: Выходной список триграммных подстрок при ведущей '*'.

    Returns:
        Строку FTS5-выражения для persons_fts, либо None, если условие вынесено
        в persons_trigram + GLOB (при ведущей '*'), либо пустую строку '', если совпадений в БД нет.
    """
    expanded_patterns = _expand_tilde_patterns(alt.text)
    fts_terms: list[str] = []

    for pat in expanded_patterns:
        if not pat:
            continue

        stripped_star = pat.rstrip("*")
        has_trailing_star = pat.endswith("*")

        # Дефисное слово без масок внутри (например, 'соколов-микитов' или 'соколов-микитов*')
        if (
            "-" in stripped_star
            and "*" not in stripped_star
            and "?" not in stripped_star
        ):
            subparts = [p for p in stripped_star.split("-") if p]
            if not subparts:
                continue
            exact_last = (alt.exact_word or alt.exact_end) and not has_trailing_star
            last_part = (
                subparts[-1]
                if (alt.exact_word or alt.exact_end or has_trailing_star)
                else _strip_flexion(subparts[-1])
            )
            prefix_parts = [f'"{_escape_fts_token(p)}"' for p in subparts[:-1]]
            last_token = (
                f'"{_escape_fts_token(last_part)}"'
                if exact_last
                else f'"{_escape_fts_token(last_part)}"*'
            )
            phrase = " ".join(prefix_parts + [last_token])
            fts_terms.append(f"({phrase})")
            continue

        # Обычное слово без '*' и '?' или со звёздочкой только на конце
        if "*" not in stripped_star and "?" not in stripped_star:
            if not stripped_star:
                return None
            if (alt.exact_word or alt.exact_end) and not has_trailing_star:
                fts_terms.append(f'"{_escape_fts_token(stripped_star)}"')
            else:
                stem = (
                    stripped_star
                    if (alt.exact_word or has_trailing_star)
                    else _strip_flexion(stripped_star)
                )
                fts_terms.append(f'"{_escape_fts_token(stem)}"*')
            continue

        # Шаблон содержит '*' или '?' в начале или середине слова
        if alt.exact_end and not has_trailing_star:
            glob_pat = pat
        else:
            if not alt.exact_word and not has_trailing_star:
                m_tail = re.search(r"([*?])([а-яa-z]{4,})$", pat)
                if m_tail:
                    pat = pat[: m_tail.start(2)] + _strip_flexion(m_tail.group(2))
            glob_pat = pat if pat.endswith("*") else f"{pat}*"

        if pat.startswith("*"):
            chunks = [
                c for c in LITERAL_CHUNKS_RE.split(pat.replace("-", " ")) if len(c) >= 3
            ]
            if chunks:
                for ch in chunks:
                    trigram_accumulator.append(f'"{_escape_fts_token(ch)}"')
                extra_globs.append((glob_cols, glob_pat))
                continue

        vocab_matches = _lookup_wildcard_vocab_terms(conn, glob_pat)
        fts_terms.extend(vocab_matches)

    if not fts_terms:
        return None if extra_globs else ""

    unique_fts = list(dict.fromkeys(fts_terms))
    if len(unique_fts) == 1:
        return f"{col_spec} : {unique_fts[0]}"
    return f"{col_spec} : ({' OR '.join(unique_fts)})"


def _build_person_sql_parts(
    conn: sqlite3.Connection,
    pf: PersonFilter,
    param_prefix: str,
) -> tuple[list[str], dict[str, Any]]:
    """Формирует список SQL-условий WHERE для таблицы persons p и словарь параметров.

    Args:
        conn: Открытое соединение SQLite.
        pf: Фильтр человека (ФИО1 или ФИО2).
        param_prefix: Уникальный префикс имён параметров ('p1' или 'p2').

    Returns:
        Кортеж (where_clauses, sql_params).

    Raises:
        _NoMatchError: Если токен запроса не встречается в словаре БД.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}

    positions = _tokenize_fio_query(pf.fio)
    mode = (pf.search_mode or "prefix_default").strip()

    if positions:
        if mode == "substring":
            trigram_pos_exprs: list[str] = []
            for pos_idx, alts in enumerate(positions):
                alt_trigram_exprs: list[str] = []
                alt_glob_patterns: list[str] = []
                all_alts_have_trigram = True

                for alt in alts:
                    for pat in _expand_tilde_patterns(alt.text):
                        clean_pat = pat.strip("*")
                        if not clean_pat:
                            continue
                        if (
                            not alt.exact_word
                            and not alt.exact_end
                            and "*" not in clean_pat
                            and "?" not in clean_pat
                        ):
                            clean_pat = _strip_flexion(clean_pat)

                        chunks = [
                            c for c in LITERAL_CHUNKS_RE.split(clean_pat) if len(c) >= 3
                        ]
                        if chunks:
                            if len(chunks) == 1:
                                alt_trigram_exprs.append(
                                    f'"{_escape_fts_token(chunks[0])}"'
                                )
                            else:
                                joined_chunks = " AND ".join(
                                    f'"{_escape_fts_token(c)}"' for c in chunks
                                )
                                alt_trigram_exprs.append(f"({joined_chunks})")
                        else:
                            all_alts_have_trigram = False

                        if "*" in clean_pat or "?" in clean_pat or not chunks:
                            alt_glob_patterns.append(f"*{clean_pat}*")

                if not alt_trigram_exprs and not alt_glob_patterns:
                    raise _NoMatchError()

                if all_alts_have_trigram and alt_trigram_exprs:
                    uniq_trig = list(dict.fromkeys(alt_trigram_exprs))
                    if len(uniq_trig) == 1:
                        trigram_pos_exprs.append(uniq_trig[0])
                    else:
                        trigram_pos_exprs.append("(" + " OR ".join(uniq_trig) + ")")

                if alt_glob_patterns or not all_alts_have_trigram:
                    glob_list = (
                        list(dict.fromkeys(alt_glob_patterns))
                        if alt_glob_patterns
                        else [
                            f"*{a.text.strip('*')}*" for a in alts if a.text.strip("*")
                        ]
                    )
                    sub_globs: list[str] = []
                    for g_idx, g_pat in enumerate(glob_list):
                        pname = f"{param_prefix}_subglob_{pos_idx}_{g_idx}"
                        params[pname] = g_pat
                        sub_globs.append(f"p.fio_norm GLOB :{pname}")
                    if sub_globs:
                        clauses.append("(" + " OR ".join(sub_globs) + ")")

            if trigram_pos_exprs:
                trig_param = f"{param_prefix}_trigram_match"
                params[trig_param] = " AND ".join(trigram_pos_exprs)
                clauses.append(
                    f"p.id IN (SELECT rowid FROM persons_trigram "
                    f"WHERE persons_trigram MATCH :{trig_param})"
                )
        else:
            # Режимы 'prefix_default' и 'prefix_no_surname'
            fts_pos_exprs: list[str] = []
            trigram_pos_exprs = []
            num_positions = len(positions)

            for pos_idx, alts in enumerate(positions):
                if mode == "prefix_default":
                    if pos_idx == 0:
                        col_spec = "surname"
                        glob_cols: tuple[str, ...] = ("surname",)
                    elif num_positions == 2 and pos_idx == 1:
                        # Для 2 слов ('Красновский Евгений' или 'Красновский Яковлевич')
                        # второе слово ищется и в имени, и в отчестве
                        col_spec = "{firstname patronymic}"
                        glob_cols = ("firstname", "patronymic")
                    elif pos_idx == 1:
                        col_spec = "firstname"
                        glob_cols = ("firstname",)
                    elif pos_idx == 2:
                        col_spec = "patronymic"
                        glob_cols = ("patronymic",)
                    else:
                        col_spec = "fio_norm"
                        glob_cols = ("fio_norm",)
                else:
                    col_spec = "fio_norm"
                    glob_cols = ("fio_norm",)

                alt_fts_clauses: list[str] = []
                extra_globs: list[tuple[tuple[str, ...], str]] = []
                trigram_acc: list[str] = []

                for alt in alts:
                    res = _build_prefix_fts_clause_for_alt(
                        conn, alt, col_spec, glob_cols, extra_globs, trigram_acc
                    )
                    if res == "":
                        continue
                    if res is not None:
                        alt_fts_clauses.append(res)

                if not alt_fts_clauses and not extra_globs:
                    raise _NoMatchError()

                if alt_fts_clauses and not extra_globs:
                    if len(alt_fts_clauses) == 1:
                        fts_pos_exprs.append(alt_fts_clauses[0])
                    else:
                        fts_pos_exprs.append("(" + " OR ".join(alt_fts_clauses) + ")")
                elif extra_globs and not alt_fts_clauses:
                    if trigram_acc:
                        trigram_pos_exprs.extend(trigram_acc)
                    g_conds: list[str] = []
                    for g_idx, (g_cols, g_pat) in enumerate(extra_globs):
                        pname = f"{param_prefix}_pglob_{pos_idx}_{g_idx}"
                        params[pname] = (
                            f"*{g_pat}"
                            if g_cols == ("fio_norm",) and not g_pat.startswith("*")
                            else g_pat
                        )
                        col_or = " OR ".join(f"p.{c} GLOB :{pname}" for c in g_cols)
                        g_conds.append(f"({col_or})" if len(g_cols) > 1 else col_or)
                    clauses.append("(" + " OR ".join(g_conds) + ")")
                else:
                    g_conds = []
                    for g_idx, (g_cols, g_pat) in enumerate(extra_globs):
                        pname = f"{param_prefix}_mglob_{pos_idx}_{g_idx}"
                        params[pname] = g_pat
                        col_or = " OR ".join(f"p.{c} GLOB :{pname}" for c in g_cols)
                        g_conds.append(f"({col_or})" if len(g_cols) > 1 else col_or)
                    for f_idx, f_expr in enumerate(alt_fts_clauses):
                        fname = f"{param_prefix}_mfts_{pos_idx}_{f_idx}"
                        params[fname] = f_expr
                        g_conds.append(
                            f"p.id IN (SELECT rowid FROM persons_fts "
                            f"WHERE persons_fts MATCH :{fname})"
                        )
                    clauses.append("(" + " OR ".join(g_conds) + ")")

            if fts_pos_exprs:
                fts_param = f"{param_prefix}_fts_match"
                params[fts_param] = " AND ".join(fts_pos_exprs)
                clauses.append(
                    f"p.id IN (SELECT rowid FROM persons_fts "
                    f"WHERE persons_fts MATCH :{fts_param})"
                )

            if trigram_pos_exprs:
                trig_param = f"{param_prefix}_ptrig_match"
                params[trig_param] = " AND ".join(trigram_pos_exprs)
                clauses.append(
                    f"p.id IN (SELECT rowid FROM persons_trigram "
                    f"WHERE persons_trigram MATCH :{trig_param})"
                )

    # Фильтры дат рождения и смерти (с учётом single_date_year / single_date_raw)
    for kind, exact_val, from_val, to_val in (
        ("birth", pf.birth_exact, pf.birth_from, pf.birth_to),
        ("death", pf.death_exact, pf.death_from, pf.death_to),
    ):
        year_expr = f"COALESCE(p.{kind}_year, p.single_date_year)"
        raw_expr = f"COALESCE(p.{kind}_date_raw, p.single_date_raw)"
        if exact_val is not None and str(exact_val).strip():
            d_spec = parse_date_filter_expr(str(exact_val))
            if d_spec is not None:
                _append_date_filter_clauses(
                    d_spec,
                    year_expr,
                    raw_expr,
                    f"{param_prefix}_{kind}",
                    clauses,
                    params,
                )
        if from_val is not None:
            pname = f"{param_prefix}_{kind}_from"
            params[pname] = int(from_val)
            clauses.append(f"{year_expr} >= :{pname}")
        if to_val is not None:
            pname = f"{param_prefix}_{kind}_to"
            params[pname] = int(to_val)
            clauses.append(f"{year_expr} <= :{pname}")

    return clauses, params


def _append_date_filter_clauses(
    spec: _DateSpec,
    year_expr: str,
    raw_expr: str,
    param_prefix: str,
    clauses: list[str],
    params: dict[str, Any],
) -> None:
    """Добавляет SQL-условия фильтрации по дате/диапазону дат с быстрым отсечением по году.

    Args:
        spec: Разобранное выражение даты или диапазона дат.
        year_expr: SQL-выражение числового года записи.
        raw_expr: SQL-выражение сырой строки даты записи.
        param_prefix: Префикс имени SQL-параметров.
        clauses: Список SQL-условий WHERE для пополнения.
        params: Словарь SQL-параметров для пополнения.
    """
    if not spec.is_range:
        b = spec.lower
        if b is None:
            return
        p_year = f"{param_prefix}_year"
        params[p_year] = b.year
        clauses.append(f"{year_expr} = :{p_year}")
        if b.month is not None or b.day is not None:
            p_spec = f"{param_prefix}_spec"
            params[p_spec] = spec.encode()
            clauses.append(f"match_date_spec({raw_expr}, {year_expr}, :{p_spec}) = 1")
        return

    lo = spec.lower
    hi = spec.upper
    if lo is not None and hi is not None and lo.year == hi.year:
        p_year = f"{param_prefix}_year"
        params[p_year] = lo.year
        clauses.append(f"{year_expr} = :{p_year}")
    else:
        if lo is not None:
            p_from = f"{param_prefix}_yfrom"
            params[p_from] = lo.year
            clauses.append(f"{year_expr} >= :{p_from}")
        if hi is not None:
            p_to = f"{param_prefix}_yto"
            params[p_to] = hi.year
            clauses.append(f"{year_expr} <= :{p_to}")

    lo_has_md = lo is not None and lo.month is not None
    hi_has_md = hi is not None and hi.month is not None
    if lo_has_md or hi_has_md:
        p_spec = f"{param_prefix}_spec"
        params[p_spec] = spec.encode()
        if (
            hi_has_md
            and not lo_has_md
            and lo is not None
            and hi is not None
            and lo.year < hi.year
        ):
            clauses.append(
                f"({year_expr} < :{param_prefix}_yto "
                f"OR match_date_spec({raw_expr}, {year_expr}, :{p_spec}) = 1)"
            )
        elif (
            lo_has_md
            and not hi_has_md
            and lo is not None
            and hi is not None
            and lo.year < hi.year
        ):
            clauses.append(
                f"({year_expr} > :{param_prefix}_yfrom "
                f"OR match_date_spec({raw_expr}, {year_expr}, :{p_spec}) = 1)"
            )
        else:
            clauses.append(f"match_date_spec({raw_expr}, {year_expr}, :{p_spec}) = 1")


def _build_plot_sql_parts(
    conn: sqlite3.Connection,
    query: SearchQuery,
) -> tuple[list[str], dict[str, Any]]:
    """Формирует список SQL-условий WHERE для таблицы plots pl и словарь параметров.

    Args:
        conn: Открытое соединение SQLite.
        query: Поисковый запрос SearchQuery.

    Returns:
        Кортеж (where_clauses, sql_params).

    Raises:
        _NoMatchError: Если указанное кладбище не найдено в справочнике.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if query.global_id is not None:
        params["plot_global_id"] = int(query.global_id)
        clauses.append("pl.global_id = :plot_global_id")

    if query.cemetery_id is not None:
        params["plot_cemetery_id"] = int(query.cemetery_id)
        clauses.append("pl.cemetery_id = :plot_cemetery_id")

    if query.cemetery_name and query.cemetery_name.strip():
        cname_norm = normalize_text(query.cemetery_name)
        cem_rows = conn.execute("SELECT id, name FROM cemeteries").fetchall()
        exact_ids = [
            int(r["id"]) for r in cem_rows if normalize_text(r["name"]) == cname_norm
        ]
        matched_cids = (
            exact_ids
            if exact_ids
            else [
                int(r["id"])
                for r in cem_rows
                if cname_norm in normalize_text(r["name"])
            ]
        )
        if not matched_cids:
            raise _NoMatchError()
        if len(matched_cids) == 1:
            params["plot_cid_0"] = matched_cids[0]
            clauses.append("pl.cemetery_id = :plot_cid_0")
        else:
            cid_ph: list[str] = []
            for i, cid in enumerate(matched_cids):
                k = f"plot_cid_{i}"
                params[k] = cid
                cid_ph.append(f":{k}")
            clauses.append(f"pl.cemetery_id IN ({','.join(cid_ph)})")

    if query.place_number and query.place_number.strip():
        pnum = query.place_number.strip()
        params["plot_sec_exact"] = pnum
        params["plot_sec_padded"] = pnum.zfill(4) if pnum.isdigit() else pnum
        params["plot_pnum_like"] = f"%{pnum}%"
        clauses.append(
            "(pl.plot_section = :plot_sec_exact "
            "OR pl.plot_section = :plot_sec_padded "
            "OR pl.place_number = :plot_sec_exact "
            "OR pl.place_number LIKE :plot_pnum_like)"
        )

    return clauses, params


def search_plots(
    db_path: str | Path,
    query: SearchQuery,
) -> SearchResponse:
    """Выполняет поисковый запрос по базе захоронений и возвращает найденные участки.

    Args:
        db_path: Путь к файлу базы данных SQLite.
        query: Типизированный поисковый запрос SearchQuery.

    Returns:
        Объект SearchResponse со списком участков, надписями и временем выполнения.
    """
    t0 = time.perf_counter()
    limit = max(1, min(int(query.limit or 50), 1000))
    offset = max(0, int(query.offset or 0))

    p1_active = query.person1 is not None and query.person1.is_active()
    p2_active = query.person2 is not None and query.person2.is_active()

    with open_search_db(db_path) as conn:
        conn.create_function(
            "match_date_spec", 3, _sqlite_match_date_spec, deterministic=True
        )
        try:
            p1_clauses, p1_params = (
                _build_person_sql_parts(conn, query.person1, "p1")
                if p1_active
                else ([], {})
            )
            p2_clauses, p2_params = (
                _build_person_sql_parts(conn, query.person2, "p2")
                if p2_active and query.person2 is not None
                else ([], {})
            )
            plot_clauses, plot_params = _build_plot_sql_parts(conn, query)
        except _NoMatchError:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return SearchResponse(
                results=[], total_plots=0, has_more=False, elapsed_ms=elapsed_ms
            )

        all_params: dict[str, Any] = {
            **p1_params,
            **p2_params,
            **plot_params,
            "limit_plus_one": limit + 1,
            "offset_val": offset,
        }

        plot_where_sql = " AND ".join(plot_clauses) if plot_clauses else "1=1"

        if p1_active and p2_active:
            p1_where = " AND ".join(p1_clauses)
            p2_where = " AND ".join(p2_clauses)
            cte_sql = f"""
            WITH m1 AS (
                SELECT
                    p.global_id,
                    GROUP_CONCAT(p.id) AS ids,
                    COUNT(*) AS cnt,
                    MIN(p.id) AS min_id
                FROM persons p
                WHERE {p1_where}
                GROUP BY p.global_id
            ),
            m2 AS (
                SELECT
                    p.global_id,
                    GROUP_CONCAT(p.id) AS ids,
                    COUNT(*) AS cnt,
                    MIN(p.id) AS min_id
                FROM persons p
                WHERE {p2_where}
                GROUP BY p.global_id
            )
            """
            base_from_sql = f"""
            {cte_sql}
            SELECT
                m1.global_id,
                m1.ids AS p1_ids,
                m2.ids AS p2_ids
            FROM m1
            JOIN m2 ON m1.global_id = m2.global_id
                AND (m1.cnt > 1 OR m2.cnt > 1 OR m1.min_id != m2.min_id)
            JOIN plots pl ON pl.global_id = m1.global_id
            WHERE {plot_where_sql}
            ORDER BY m1.global_id
            """
            count_sql = f"""
            {cte_sql}
            SELECT COUNT(*) FROM (
                SELECT m1.global_id
                FROM m1
                JOIN m2 ON m1.global_id = m2.global_id
                    AND (m1.cnt > 1 OR m2.cnt > 1 OR m1.min_id != m2.min_id)
                JOIN plots pl ON pl.global_id = m1.global_id
                WHERE {plot_where_sql}
                LIMIT :count_cap
            )
            """
        elif p1_active or p2_active:
            active_clauses = p1_clauses if p1_active else p2_clauses
            p_where = " AND ".join(active_clauses)
            col_alias = "p1_ids" if p1_active else "p2_ids"
            other_alias = "p2_ids" if p1_active else "p1_ids"
            join_plots = (
                "JOIN plots pl ON pl.global_id = p.global_id" if plot_clauses else ""
            )
            extra_where = f" AND {plot_where_sql}" if plot_clauses else ""
            base_from_sql = f"""
            SELECT
                p.global_id,
                GROUP_CONCAT(p.id) AS {col_alias},
                NULL AS {other_alias}
            FROM persons p
            {join_plots}
            WHERE {p_where}{extra_where}
            GROUP BY p.global_id
            ORDER BY p.global_id
            """
            count_sql = f"""
            SELECT COUNT(*) FROM (
                SELECT p.global_id
                FROM persons p
                {join_plots}
                WHERE {p_where}{extra_where}
                GROUP BY p.global_id
                LIMIT :count_cap
            )
            """
        else:
            base_from_sql = f"""
            SELECT
                pl.global_id,
                NULL AS p1_ids,
                NULL AS p2_ids
            FROM plots pl
            WHERE {plot_where_sql}
            ORDER BY pl.global_id
            """
            count_sql = f"""
            SELECT COUNT(*) FROM (
                SELECT pl.global_id
                FROM plots pl
                WHERE {plot_where_sql}
                LIMIT :count_cap
            )
            """

        gid_sql = f"{base_from_sql} LIMIT :limit_plus_one OFFSET :offset_val"
        gid_rows = conn.execute(gid_sql, all_params).fetchall()
        has_more = len(gid_rows) > limit
        page_rows = gid_rows[:limit]
        page_gids = [int(r["global_id"]) for r in page_rows]

        if not page_gids:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return SearchResponse(
                results=[], total_plots=0, has_more=False, elapsed_ms=elapsed_ms
            )

        matched_p1_ids: set[int] = set()
        matched_p2_ids: set[int] = set()
        for r in page_rows:
            if r["p1_ids"]:
                for pid_str in str(r["p1_ids"]).split(","):
                    if pid_str:
                        matched_p1_ids.add(int(pid_str))
            if r["p2_ids"]:
                for pid_str in str(r["p2_ids"]).split(","):
                    if pid_str:
                        matched_p2_ids.add(int(pid_str))

        if not has_more:
            total_plots = offset + len(page_gids)
        else:
            count_cap = max(10001, offset + limit + 1)
            raw_total = int(
                conn.execute(
                    count_sql, {**all_params, "count_cap": count_cap}
                ).fetchone()[0]
            )
            total_plots = min(raw_total, 10000) if raw_total > 10000 else raw_total

        # Загрузка атрибутов найденных участков
        gid_ph = ",".join("?" for _ in page_gids)
        plot_rows = conn.execute(
            f"""
            SELECT
                pl.global_id,
                pl.cemetery_id,
                c.name AS cemetery_name,
                pl.place_number,
                pl.plot_section,
                pl.grave_type,
                pl.has_tombstone,
                pl.person_count,
                pl.lon,
                pl.lat,
                pl.area
            FROM plots pl
            JOIN cemeteries c ON c.id = pl.cemetery_id
            WHERE pl.global_id IN ({gid_ph})
            ORDER BY pl.global_id
            """,
            page_gids,
        ).fetchall()

        # Загрузка всех надписей на найденных участках
        person_rows = conn.execute(
            f"""
            SELECT
                id,
                global_id,
                raw_line,
                fio_norm,
                birth_date_raw,
                death_date_raw,
                single_date_raw,
                birth_year,
                death_year,
                single_date_year
            FROM persons
            WHERE global_id IN ({gid_ph})
            ORDER BY global_id, id
            """,
            page_gids,
        ).fetchall()

        persons_by_gid: dict[int, list[PersonResult]] = {}
        for pr in person_rows:
            pid = int(pr["id"])
            gid = int(pr["global_id"])
            fio_disp, dates_disp = split_display_inscription(
                pr["raw_line"],
                pr["birth_date_raw"],
                pr["death_date_raw"],
                pr["single_date_raw"],
            )
            persons_by_gid.setdefault(gid, []).append(
                PersonResult(
                    id=pid,
                    raw_line=pr["raw_line"],
                    fio_norm=pr["fio_norm"],
                    birth_date_raw=pr["birth_date_raw"],
                    death_date_raw=pr["death_date_raw"],
                    single_date_raw=pr["single_date_raw"],
                    birth_year=pr["birth_year"],
                    death_year=pr["death_year"],
                    single_date_year=pr["single_date_year"],
                    matched_p1=pid in matched_p1_ids,
                    matched_p2=pid in matched_p2_ids,
                    fio_display=fio_disp,
                    dates_display=dates_disp,
                )
            )

        results: list[PlotResult] = []
        for row in plot_rows:
            gid = int(row["global_id"])
            cname = str(row["cemetery_name"])
            gtype = int(row["grave_type"])
            glabel = format_grave_type(gtype)
            lon = float(row["lon"]) if row["lon"] is not None else None
            lat = float(row["lat"]) if row["lat"] is not None else None
            area = float(row["area"]) if row["area"] is not None else None
            ya_url, google_url = build_urls(gid, lon, lat)
            _, osm_url, nakarte_url = build_extra_map_urls(lon, lat)
            epoisk_map_url = build_epoisk_map_url(gid, cname)

            results.append(
                PlotResult(
                    global_id=gid,
                    cemetery_id=int(row["cemetery_id"]),
                    cemetery_name=cname,
                    place_number=row["place_number"],
                    plot_section=row["plot_section"],
                    grave_type=gtype,
                    grave_type_label=glabel,
                    has_tombstone=int(row["has_tombstone"]),
                    person_count=int(row["person_count"]),
                    lon=lon,
                    lat=lat,
                    area=area,
                    ya_url=ya_url,
                    google_url=google_url,
                    osm_url=osm_url,
                    nakarte_url=nakarte_url,
                    epoisk_map_url=epoisk_map_url,
                    persons=persons_by_gid.get(gid, []),
                )
            )

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return SearchResponse(
        results=results,
        total_plots=total_plots,
        has_more=has_more,
        elapsed_ms=elapsed_ms,
    )


def get_db_meta(db_path: str | Path) -> dict[str, Any]:
    """Возвращает общую статистику базы данных и список кладбищ.

    Args:
        db_path: Путь к файлу базы данных SQLite.

    Returns:
        Словарь с общим числом участков, надписей и списком кладбищ.
    """
    with open_search_db(db_path) as conn:
        total_plots = int(conn.execute("SELECT COUNT(*) FROM plots").fetchone()[0])
        total_persons = int(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0])
        cem_rows = conn.execute("""
            SELECT c.id, c.code, c.name, COUNT(pl.global_id) AS plot_count
            FROM cemeteries c
            LEFT JOIN plots pl ON pl.cemetery_id = c.id
            GROUP BY c.id
            ORDER BY c.name
            """).fetchall()

        cemeteries = [
            {
                "id": int(r["id"]),
                "code": r["code"],
                "name": r["name"],
                "plot_count": int(r["plot_count"]),
            }
            for r in cem_rows
        ]

    return {
        "total_plots": total_plots,
        "total_persons": total_persons,
        "cemeteries": cemeteries,
    }
