"""Чистые функции разбора шифров участков, надписей на памятниках и дат."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from src.models import ParsedPerson

DATE_TOKEN_RE = re.compile(
    r"^(?:\d{2}\.\d{2}\.\d{4}|год\s*[\d*]+|месяц\s+\d{2}|день\s+\d{2})$"
)
YEAR_TOKEN_RE = re.compile(r"^год\s*(\d{4})$")
FULL_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.(\d{4})$")
WORD_RE = re.compile(r"[а-яa-z0-9]+(?:-[а-яa-z0-9]+)*")
WHITESPACE_RE = re.compile(r"\s+")

COLUMBARIUM_PREFIXES = frozenset({"КО", "КСФ", "КЗ", "К"})

GRAVE_TYPE_TO_CODE: dict[str, int] = {
    "гробом в землю": 1,
    "1": 1,
    "урной в открытый колумбарий": 2,
    "2": 2,
    "урной в землю": 3,
    "3": 3,
}

GRAVE_TYPE_LABELS: dict[int, str] = {
    1: "гробом в землю",
    2: "урной в открытый колумбарий",
    3: "урной в землю",
}


def normalize_text(text: str) -> str:
    """Приводит строку к нижнему регистру, заменяет 'ё' на 'е' и схлопывает пробелы.

    Args:
        text: Исходная строка.

    Returns:
        Нормализованная строка.
    """
    if not text:
        return ""
    cleaned = text.lower().replace("ё", "е")
    return WHITESPACE_RE.sub(" ", cleaned).strip()


def parse_grave_type(raw_type: str | int | None) -> int:
    """Преобразует строковое название или код типа захоронения в числовой код (1, 2, 3).

    Args:
        raw_type: Значение поля GraveType из входного файла.

    Returns:
        Числовой код типа захоронения (по умолчанию 1).
    """
    if raw_type is None:
        return 1
    if isinstance(raw_type, int):
        return raw_type if raw_type in GRAVE_TYPE_LABELS else 1
    return GRAVE_TYPE_TO_CODE.get(raw_type.strip(), 1)


def format_grave_type(grave_type: int) -> str:
    """Возвращает человекочитаемое описание для кода типа захоронения.

    Args:
        grave_type: Числовой код (1, 2 или 3).

    Returns:
        Текстовое описание типа захоронения.
    """
    return GRAVE_TYPE_LABELS.get(grave_type, "гробом в землю")


def parse_has_tombstone(
    raw_val: str | int | bool | None, has_inscriptions: bool
) -> int:
    """Определяет флаг наличия надмогильного сооружения (1 или 0).

    Args:
        raw_val: Значение поля HasTombstone ('да'/'нет').
        has_inscriptions: Присутствуют ли непустые надписи на участке.

    Returns:
        1 при наличии памятника или надписей, иначе 0.
    """
    if has_inscriptions:
        return 1
    if raw_val is None:
        return 0
    if isinstance(raw_val, (int, bool)):
        return 1 if raw_val else 0
    val = str(raw_val).strip().lower()
    return 1 if val in ("да", "yes", "true", "1") else 0


def parse_place_number(place_number: str | None) -> tuple[str | None, str | None]:
    """Извлекает 3-значный код кладбища и номер участка/колумбария из шифра PlaceNumber.

    Args:
        place_number: Полный шифр места захоронения (например, 'ЗИ/002/3.3/1667').

    Returns:
        Кортеж (cemetery_code, plot_section).
    """
    if not place_number:
        return None, None
    cleaned = place_number.strip()
    if not cleaned:
        return None, None
    parts = cleaned.split("/")
    if len(parts) < 2:
        return None, None

    prefix = parts[0].strip()
    cemetery_code = parts[1].strip() or None

    if prefix in COLUMBARIUM_PREFIXES:
        plot_section = (
            parts[2].strip() if len(parts) >= 3 and parts[2].strip() else None
        )
    elif len(parts) >= 4:
        plot_section = "/".join(p.strip() for p in parts[2:-1]) or None
    elif len(parts) == 3:
        plot_section = parts[2].strip() or None
    else:
        plot_section = None

    return cemetery_code, plot_section


def split_inscription_cell(cell_text: str | None) -> list[str]:
    """Разбивает мультистрочное значение ячейки Inscription из таблицы в список строк.

    Args:
        cell_text: Сырое содержимое ячейки Inscription (с блоками 'Inscription:...\\n\\n').

    Returns:
        Список очищенных непустых строк надписей без префикса 'Inscription:'.
    """
    if not cell_text:
        return []
    normalized = cell_text.replace("\r\n", "\n")
    raw_blocks = normalized.split("\n\n")
    result: list[str] = []
    for block in raw_blocks:
        line = block.strip()
        if not line:
            continue
        if line.startswith("Inscription:"):
            line = line[len("Inscription:") :].strip()
        if line:
            result.append(line)
    return result


def deduplicate_inscriptions(lines: list[str]) -> list[str]:
    """Выполняет регистронезависимую префиксную дедупликацию надписей на одном участке.

    Если строка A без учёта регистра (и с заменой ё -> е) является префиксом строки B
    на том же участке, оставляется более полная строка B. При полном совпадении без учёта
    регистра предпочтение отдаётся строке в смешанном регистре перед строкой КАПСОМ.

    Args:
        lines: Исходный список строк надписей на участке.

    Returns:
        Дедуплицированный список строк с сохранением порядка.
    """
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if len(cleaned) <= 1:
        return cleaned

    norms = [line.lower().replace("ё", "е") for line in cleaned]

    # Шаг 1: схлопывание точных регистронезависимых дублей
    unique_lines: list[str] = []
    unique_norms: list[str] = []
    norm_to_idx: dict[str, int] = {}

    for line, norm in zip(cleaned, norms):
        if norm in norm_to_idx:
            idx = norm_to_idx[norm]
            existing = unique_lines[idx]
            if existing.isupper() and not line.isupper():
                unique_lines[idx] = line
        else:
            norm_to_idx[norm] = len(unique_lines)
            unique_lines.append(line)
            unique_norms.append(norm)

    if len(unique_lines) <= 1:
        return unique_lines

    # Шаг 2: отбрасывание строк, являющихся строгим префиксом другой строки участка
    result: list[str] = []
    n = len(unique_lines)
    for i in range(n):
        norm_i = unique_norms[i]
        len_i = len(norm_i)
        is_strict_prefix = False
        for j in range(n):
            if i != j and len(unique_norms[j]) > len_i:
                if unique_norms[j].startswith(norm_i):
                    is_strict_prefix = True
                    break
        if not is_strict_prefix:
            result.append(unique_lines[i])

    return result


def _classify_date_token(token: str) -> tuple[int, int | None]:
    """Определяет ранг примитива даты и извлекает числовой год (если есть).

    Args:
        token: Токен даты ('DD.MM.YYYY', 'год YYYY', 'месяц MM', 'день DD').

    Returns:
        Кортеж (rank, year), где rank: 1=день, 2=месяц, 3=год, 99=полная дата.
    """
    m_full = FULL_DATE_RE.match(token)
    if m_full:
        year = int(m_full.group(1))
        return 99, (year if year > 0 else None)

    if token.startswith("год"):
        m_year = YEAR_TOKEN_RE.match(token)
        if m_year:
            year = int(m_year.group(1))
            return 3, (year if year > 0 else None)
        return 3, None

    if token.startswith("месяц"):
        return 2, None

    return 1, None


def _group_date_tokens(date_tokens: list[str]) -> list[tuple[str, int | None]]:
    """Группирует последовательность примитивов дат в логические даты (рождения / смерти).

    Каждая логическая дата состоит либо из одного токена DD.MM.YYYY, либо из 1–2 токенов
    в строго возрастающем порядке единиц (день -> месяц, день -> год, месяц -> год).

    Args:
        date_tokens: Список токенов дат в порядке слева направо.

    Returns:
        Список кортежей (raw_date_str, year_int_or_none).
    """
    if not date_tokens:
        return []

    grouped: list[tuple[str, int | None]] = []
    curr_tokens: list[str] = []
    curr_rank = 0
    curr_year: int | None = None

    for token in date_tokens:
        rank, year = _classify_date_token(token)
        if len(curr_tokens) == 1 and curr_rank < rank <= 3:
            curr_tokens.append(token)
            curr_rank = 99
            if year is not None:
                curr_year = year
            grouped.append((",".join(curr_tokens), curr_year))
            curr_tokens = []
            curr_year = None
        else:
            if curr_tokens:
                grouped.append((",".join(curr_tokens), curr_year))
            if rank == 99:
                grouped.append((token, year))
                curr_tokens = []
                curr_rank = 0
                curr_year = None
            else:
                curr_tokens = [token]
                curr_rank = rank
                curr_year = year

    if curr_tokens:
        grouped.append((",".join(curr_tokens), curr_year))

    return grouped


def _split_trailing_dates(line: str) -> tuple[str, list[str]]:
    """Отщепляет справа налево токены дат от строки надписи.

    Args:
        line: Исходная строка надписи без префикса 'Inscription:'.

    Returns:
        Кортеж (fio_part, date_tokens), где date_tokens упорядочены слева направо.
    """
    parts = line.strip().split(",")
    date_tokens_rev: list[str] = []
    while parts and DATE_TOKEN_RE.match(parts[-1].strip()):
        date_tokens_rev.append(parts.pop().strip())
    return ",".join(parts).strip(), list(reversed(date_tokens_rev))


def parse_inscription_line(raw_line: str) -> ParsedPerson:
    """Разбирает строку надписи справа налево на ФИО и даты согласно грамматике SPEC.md.

    Args:
        raw_line: Строка надписи без префикса 'Inscription:'.

    Returns:
        Структура ParsedPerson с выделенными ФИО, нормализованным текстом и датами.
    """
    line = raw_line.strip()
    fio_raw, date_tokens = _split_trailing_dates(line)
    logical_dates = _group_date_tokens(date_tokens)
    fio_norm = normalize_text(fio_raw)
    words = WORD_RE.findall(fio_norm)

    surname = words[0] if len(words) > 0 else None
    firstname = words[1] if len(words) > 1 else None
    patronymic = words[2] if len(words) > 2 else None

    birth_year: int | None = None
    birth_date_raw: str | None = None
    death_year: int | None = None
    death_date_raw: str | None = None
    single_date_year: int | None = None
    single_date_raw: str | None = None

    if len(logical_dates) == 1:
        single_date_raw, single_date_year = logical_dates[0]
    elif len(logical_dates) >= 2:
        birth_date_raw, birth_year = logical_dates[0]
        death_date_raw, death_year = logical_dates[1]

    return ParsedPerson(
        raw_line=line,
        surname=surname,
        firstname=firstname,
        patronymic=patronymic,
        fio_norm=fio_norm,
        birth_year=birth_year,
        birth_date_raw=birth_date_raw,
        death_year=death_year,
        death_date_raw=death_date_raw,
        single_date_year=single_date_year,
        single_date_raw=single_date_raw,
    )


def _polygon_ring_area_m2(ring: list[list[float]]) -> float:
    """Вычисляет приближённую площадь кольца полигона WGS84 в квадратных метрах.

    Args:
        ring: Список координатных пар [lon, lat] внешнего контура полигона.

    Returns:
        Приближённая площадь полигона в квадратных метрах (0.0 при некорректных данных).
    """
    if len(ring) < 3:
        return 0.0
    try:
        lat0_rad = math.radians(float(ring[0][1]))
        m_per_deg_lat = 111_132.92
        m_per_deg_lon = 111_412.84 * math.cos(lat0_rad)

        area = 0.0
        n = len(ring)
        for i in range(n):
            x1 = float(ring[i][0]) * m_per_deg_lon
            y1 = float(ring[i][1]) * m_per_deg_lat
            x2 = float(ring[(i + 1) % n][0]) * m_per_deg_lon
            y2 = float(ring[(i + 1) % n][1]) * m_per_deg_lat
            area += x1 * y2 - x2 * y1
        return abs(area) * 0.5
    except (ValueError, TypeError, IndexError):
        return 0.0


def _polygon_ring_centroid(
    ring: list[list[float]],
) -> tuple[float | None, float | None]:
    """Вычисляет приближённый центроид (lon, lat) внешнего кольца полигона.

    Args:
        ring: Список вершин [lon, lat] кольца полигона.

    Returns:
        Кортеж (lon, lat), округлённый до 6 знаков, или (None, None).
    """
    if not ring:
        return None, None
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    if not pts:
        return None, None
    try:
        lon = round(sum(float(pt[0]) for pt in pts) / len(pts), 6)
        lat = round(sum(float(pt[1]) for pt in pts) / len(pts), 6)
        return lon, lat
    except (ValueError, TypeError, IndexError):
        return None, None


def _parse_geo_obj(raw: dict[str, Any] | str | None) -> dict[str, Any] | None:
    """Преобразует словарь или строковую сериализацию geoData (JSON или Apache POI) в dict.

    Args:
        raw: Словарь GeoJSON или строковое представление геометрии.

    Returns:
        Разобранный словарь с ключами 'type' и 'coordinates' либо None.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.startswith('{"') or s.startswith("{ "):
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
    c_idx = s.find("coordinates=")
    if c_idx != -1:
        r_idx = s.rfind("]")
        if r_idx > c_idx:
            try:
                coords = json.loads(s[c_idx + 12 : r_idx + 1])
            except json.JSONDecodeError:
                return None
            gtype = "Polygon"
            t_idx = s.find("type=", r_idx)
            if t_idx == -1:
                t_idx = s.find("type=")
            if t_idx != -1:
                tail = s[t_idx + 5 :].rstrip("}")
                gtype = tail.split(",", 1)[0].strip()
            return {"type": gtype, "coordinates": coords}
    return None


def extract_geo_info(
    geo_data: dict[str, Any] | str | None,
    geodata_center: dict[str, Any] | str | None = None,
) -> tuple[float | None, float | None, float | None]:
    """Извлекает координаты (lon, lat) и площадь (area) из полей geoData / geodata_center.

    Поддерживает как словари GeoJSON, так и строковое представление Apache POI из .xlsx/.csv.

    Args:
        geo_data: Поле geoData (Polygon / MultiPolygon / Point).
        geodata_center: Поле geodata_center (Point).

    Returns:
        Кортеж (lon, lat, area).
    """
    lon: float | None = None
    lat: float | None = None
    area: float | None = None

    center_obj = _parse_geo_obj(geodata_center)
    if isinstance(center_obj, dict):
        coords = center_obj.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            try:
                lon = round(float(coords[0]), 6)
                lat = round(float(coords[1]), 6)
            except (ValueError, TypeError):
                lon, lat = None, None

    geo_obj = _parse_geo_obj(geo_data)
    if isinstance(geo_obj, dict):
        gtype = geo_obj.get("type")
        coords = geo_obj.get("coordinates")
        if gtype == "Point" and isinstance(coords, list) and len(coords) >= 2:
            if lon is None:
                try:
                    lon = round(float(coords[0]), 6)
                    lat = round(float(coords[1]), 6)
                except (ValueError, TypeError):
                    lon, lat = None, None
        elif gtype == "Polygon" and isinstance(coords, list) and coords:
            outer = coords[0]
            if isinstance(outer, list) and outer:
                if lon is None:
                    lon, lat = _polygon_ring_centroid(outer)
                raw_area = _polygon_ring_area_m2(outer)
                if raw_area > 0:
                    area = round(raw_area, 2)
        elif gtype == "MultiPolygon" and isinstance(coords, list) and coords:
            total_area = 0.0
            first_ring = None
            for poly in coords:
                if isinstance(poly, list) and poly and isinstance(poly[0], list):
                    if first_ring is None:
                        first_ring = poly[0]
                    total_area += _polygon_ring_area_m2(poly[0])
            if lon is None and first_ring:
                lon, lat = _polygon_ring_centroid(first_ring)
            if total_area > 0:
                area = round(total_area, 2)

    return lon, lat, area


RUS_TO_EPOISK_SLUG: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}

_SLUG_SEP_RE = re.compile(r"[^a-z0-9]+")


def cemetery_name_to_epoisk_slug(cemetery_name: str | None) -> str | None:
    """Транслитерирует русское название кладбища в URL-слаг схемы epoisk.ru/burmsk/maps/.

    Args:
        cemetery_name: Название кладбища (например, 'Перловское' или 'Востряковское (Центральное)').

    Returns:
        Латинский слаг (например, 'perlovskoe' или 'vostryakovskoe-tsentralnoe') либо None.
    """
    if not cemetery_name:
        return None
    translit = "".join(RUS_TO_EPOISK_SLUG.get(ch, ch) for ch in cemetery_name.lower())
    slug = _SLUG_SEP_RE.sub("-", translit).strip("-")
    return slug or None


def build_epoisk_map_url(global_id: int, cemetery_name: str | None) -> str | None:
    """Формирует прямую ссылку на интерактивную схему кладбища epoisk.ru с подсветкой участка.

    Args:
        global_id: Идентификатор участка (global_id).
        cemetery_name: Название кладбища.

    Returns:
        URL схемы кладбища с параметром ?gid=... или None.
    """
    slug = cemetery_name_to_epoisk_slug(cemetery_name)
    if not slug:
        return None
    return f"https://epoisk.ru/burmsk/cemetery/{slug}/map/?gid={global_id}"


def build_extra_map_urls(
    lon: float | None, lat: float | None
) -> tuple[str | None, str | None, str | None]:
    """Формирует ссылки на Google Maps, OpenStreetMap и nakarte.me (Bing Satellite).

    Args:
        lon: Долгота участка (если известна).
        lat: Широта участка (если известна).

    Returns:
        Кортеж (google_url, osm_url, nakarte_url).
    """
    if lon is None or lat is None:
        return None, None, None
    google_url = f"https://www.google.com/maps?q={lat},{lon}&ll={lat},{lon}&z=18"
    osm_url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=19/{lat}/{lon}"
    nakarte_url = f"https://nakarte.me/#m=18/{lat}/{lon}&l=I"
    return google_url, osm_url, nakarte_url


def build_urls(
    global_id: int, lon: float | None, lat: float | None
) -> tuple[str | None, str | None]:
    """Формирует детерминированные ссылки на Яндекс.Карты (домен yandex.ru) и Google Maps.

    Args:
        global_id: Идентификатор участка.
        lon: Долгота участка (если известна).
        lat: Широта участка (если известна).

    Returns:
        Кортеж (ya_url, google_url).
    """
    _ = global_id
    if lon is None or lat is None:
        return None, None
    ya_url = (
        f"https://yandex.ru/maps/?ll={lon}%2C{lat}"
        f"&lang=ru&mode=search&text={lat}%2C{lon}&z=17"
    )
    google_url = f"https://www.google.com/maps?q={lat},{lon}&ll={lat},{lon}&z=18"
    return ya_url, google_url


def format_raw_date(raw_date: str | None) -> str:
    """Преобразует сырое представление даты из БД в компактную читаемую строку.

    Примеры:
        '16.03.1954' -> '16.03.1954'
        'год 1894' -> '1894'
        'месяц 06,год 1897' -> '06.1897'
        'день 15,год 1922' -> '15.**.1922'
        'день 08,месяц 02' -> '08.02.****'

    Args:
        raw_date: Сырая строка даты (birth_date_raw, death_date_raw или single_date_raw).

    Returns:
        Читаемая строка даты или пустая строка.
    """
    if not raw_date:
        return ""
    tokens = [t.strip() for t in raw_date.split(",") if t.strip()]
    if not tokens:
        return ""
    if len(tokens) == 1 and "." in tokens[0]:
        return tokens[0]

    day: str | None = None
    month: str | None = None
    year: str | None = None
    for tok in tokens:
        if tok.startswith("год"):
            year = tok[3:].strip()
        elif tok.startswith("месяц"):
            month = tok[5:].strip()
        elif tok.startswith("день"):
            day = tok[4:].strip()

    if year and not month and not day:
        return year
    if month and year and not day:
        return f"{month}.{year}"
    if day or month or year:
        return f"{day or '**'}.{month or '**'}.{year or '****'}"
    return ", ".join(tokens)


def split_display_inscription(
    raw_line: str,
    birth_date_raw: str | None = None,
    death_date_raw: str | None = None,
    single_date_raw: str | None = None,
) -> tuple[str, str]:
    """Разделяет строку надписи на исходное ФИО и читаемую строку дат.

    Args:
        raw_line: Исходная строка надписи из базы данных.
        birth_date_raw: Сырая строка даты рождения (если есть).
        death_date_raw: Сырая строка даты смерти (если есть).
        single_date_raw: Сырая строка единственной даты (если есть).

    Returns:
        Кортеж (fio_display, dates_display).
    """
    fio_raw, _ = _split_trailing_dates(raw_line)
    fio_display = fio_raw.strip(" ,")

    b_fmt = format_raw_date(birth_date_raw)
    d_fmt = format_raw_date(death_date_raw)
    s_fmt = format_raw_date(single_date_raw)

    if b_fmt and d_fmt:
        dates_display = f"{b_fmt} – {d_fmt}"
    elif b_fmt or d_fmt:
        dates_display = f"{b_fmt or '?'} – {d_fmt or '?'}"
    elif s_fmt:
        dates_display = s_fmt
    else:
        dates_display = ""

    return fio_display, dates_display


def format_display_inscription(
    raw_line: str,
    birth_date_raw: str | None = "",
    death_date_raw: str | None = "",
    single_date_raw: str | None = "",
) -> str:
    """Форматирует надпись на памятнике в единую строку вида 'ФИО (даты)' для CLI и CSV.

    Args:
        raw_line: Исходная строка надписи из базы данных.
        birth_date_raw: Сырая строка даты рождения (если есть).
        death_date_raw: Сырая строка даты смерти (если есть).
        single_date_raw: Сырая строка единственной даты (если есть).

    Returns:
        Строка вида 'ФИО (даты)' или 'ФИО' (с подстановкой 'ФИО не указано' при пустом ФИО).
    """
    fio_disp, dates_disp = split_display_inscription(
        raw_line,
        birth_date_raw or None,
        death_date_raw or None,
        single_date_raw or None,
    )
    fio_part = fio_disp or "ФИО не указано"
    return f"{fio_part} ({dates_disp})" if dates_disp else fio_part
