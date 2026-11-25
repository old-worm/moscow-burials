"""Потоковый импорт и идемпотентное слияние (UPSERT) выгрузок реестра захоронений."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import functools
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, BinaryIO
import xml.etree.ElementTree as ET
import zipfile
import zlib

from src.db import (
    ensure_db_initialized,
    open_import_db,
    open_search_db,
    rebuild_fts_indexes,
)
from src.models import ImportStats, PlotRecord
from src.parser import (
    deduplicate_inscriptions,
    extract_geo_info,
    parse_grave_type,
    parse_has_tombstone,
    parse_inscription_line,
    parse_place_number,
    split_inscription_cell,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 2000
SUPPORTED_SOURCE_EXTS = frozenset({".xlsx", ".json", ".csv", ".xml"})
FILENAME_DMY_RE = re.compile(r"(?<!\d)(\d{2})\.(\d{2})\.(20\d{2})(?!\d)")
FILENAME_YMD_RE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")
ISO_DATE_PREFIX_RE = re.compile(r"^(20\d{2}-\d{2}-\d{2})")

RU_HEADER_ALIASES: dict[str, str] = {
    "Номер места (участка) захоронения": "PlaceNumber",
    "Шифр места": "PlaceNumber",
    "Тип захоронения на участке": "GraveType",
    "Тип захоронения": "GraveType",
    "Наличие надмогильного сооружения": "HasTombstone",
    "Надпись на надмогильном сооружении": "Inscription",
    "Надписи на памятнике": "Inscription",
    "Кладбище": "Cemetery",
    "global_id": "global_id",
}
_RU_HEADER_ALIASES_LOWER: dict[str, str] = {
    k.lower(): v for k, v in RU_HEADER_ALIASES.items()
}


class DataValidationError(ValueError):
    """Ошибка валидации формата, структуры или целостности входного файла данных."""


class ImportCancelledError(Exception):
    """Импорт остановлен по запросу пользователя; изменения в базе отменены."""


class ImportCancelToken:
    """Потокобезопасный признак отмены импорта.

    cancel() можно вызывать из любого потока: он только выставляет флаг. Соединение
    импорта проверяет флаг через SQLite progress handler каждые
    PROGRESS_CHECK_OPCODES инструкций виртуальной машины, поэтому даже многоминутное
    перестроение FTS-индекса прерывается почти сразу, без гонок и без обращения
    к соединению из чужого потока.
    """

    PROGRESS_CHECK_OPCODES = 100_000

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        """Возвращает True, если отмена уже запрошена."""
        return self._event.is_set()

    def cancel(self) -> None:
        """Запрашивает отмену импорта."""
        self._event.set()

    def attach(self, conn: sqlite3.Connection) -> None:
        """Включает прерывание SQL-запросов соединения при запросе отмены."""
        conn.set_progress_handler(self._event.is_set, self.PROGRESS_CHECK_OPCODES)

    @staticmethod
    def detach(conn: sqlite3.Connection) -> None:
        """Отключает проверку отмены у соединения."""
        conn.set_progress_handler(None, 0)

    def raise_if_cancelled(self) -> None:
        """Бросает ImportCancelledError, если отмена запрошена.

        Raises:
            ImportCancelledError: Пользователь остановил импорт.
        """
        if self.cancelled:
            raise ImportCancelledError("Импорт остановлен пользователем, изменения в базе отменены.")


@contextmanager
def _cancellable(
    conn: sqlite3.Connection, cancel_token: ImportCancelToken | None
) -> Iterator[None]:
    """Подключает соединение к признаку отмены и переводит прерванный SQL-запрос в ImportCancelledError.

    Args:
        conn: Соединение импорта.
        cancel_token: Признак отмены или None (тогда ничего не делает).

    Raises:
        ImportCancelledError: SQL-запрос прерван вызовом cancel_token.cancel().
    """
    if cancel_token is None:
        yield
        return
    cancel_token.attach(conn)
    try:
        yield
    except sqlite3.OperationalError as exc:
        if cancel_token.cancelled:
            raise ImportCancelledError(
                "Импорт остановлен пользователем, изменения в базе отменены."
            ) from exc
        raise
    finally:
        cancel_token.detach(conn)


def _normalize_header_name(raw_name: str) -> str:
    """Приводит заголовок колонки (включая русские названия) к каноническому имени схемы.

    Args:
        raw_name: Исходное название колонки из первой строки таблицы.

    Returns:
        Каноническое имя поля ('global_id', 'PlaceNumber', 'Cemetery' и т.д.).
    """
    cleaned = raw_name.strip().lstrip("\ufeff").strip()
    if cleaned in RU_HEADER_ALIASES:
        return RU_HEADER_ALIASES[cleaned]
    lowered = cleaned.lower()
    if lowered in _RU_HEADER_ALIASES_LOWER:
        return _RU_HEADER_ALIASES_LOWER[lowered]
    return cleaned


def validate_source_file(
    file_path: str | Path,
    display_name: str | None = None,
) -> str:
    """Проверяет существование, непустоту и сигнатуру формата входного файла (Фаза 1).

    Распознаёт неподдерживаемые архивы (.rar, .7z, .gz, нераспакованный .zip),
    старый формат Excel 97–2003 (.xls), документы PDF, сохранённые веб-страницы HTML
    и повреждённые ZIP/XLSX-контейнеры.

    Args:
        file_path: Путь к проверяемому файлу на диске.
        display_name: Отображаемое имя файла для сообщений об ошибках (если отличается от имени пути).

    Returns:
        Строковый код распознанного формата: 'xlsx', 'json', 'csv' или 'xml'.

    Raises:
        DataValidationError: Если файл отсутствует, пуст, повреждён или имеет неподдерживаемый формат.
    """
    path = Path(file_path)
    name = (display_name or path.name).strip() or path.name
    if not path.is_file():
        raise DataValidationError(f"Файл не найден: «{name}».")

    try:
        st_size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(4096)
            is_all_ws = head.lstrip(b"\xef\xbb\xbf\xff\xfe\xfe\xff").strip() == b""
            if is_all_ws and st_size > 4096:
                while chunk := f.read(65536):
                    if chunk.strip():
                        is_all_ws = False
                        break
    except OSError as exc:
        raise DataValidationError(f"Не удалось прочитать файл «{name}»: {exc}") from exc

    if st_size == 0 or is_all_ws:
        raise DataValidationError(
            f"Файл «{name}» пуст (0 байт). Скачайте полный файл выгрузки (.xlsx, .csv, .json или .xml)."
        )

    suffix = Path(name).suffix.lower() or path.suffix.lower()

    if head.startswith(b"Rar!\x1a\x07") or suffix == ".rar":
        raise DataValidationError(
            f"Файл «{name}» является архивом RAR. Распакуйте архив и загрузите "
            f"находящийся внутри файл (.xlsx, .csv, .json или .xml)."
        )

    if head.startswith(b"7z\xbc\xaf\x27\x1c") or suffix == ".7z":
        raise DataValidationError(
            f"Файл «{name}» является архивом 7-Zip (.7z). Распакуйте архив и загрузите "
            f"находящийся внутри файл (.xlsx, .csv, .json или .xml)."
        )

    if head.startswith(b"\x1f\x8b") or suffix == ".gz":
        raise DataValidationError(
            f"Файл «{name}» сжат в формат GZIP (.gz). Распакуйте архив перед импортом."
        )

    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") or suffix == ".xls":
        raise DataValidationError(
            f"Файл «{name}» имеет устаревший бинарный формат Excel 97–2003 (.xls). "
            f"Пересохраните его в Excel в формате «Книга Excel (.xlsx)» или «CSV (разделители — запятые)»."
        )

    if head.startswith(b"%PDF-") or suffix == ".pdf":
        raise DataValidationError(
            f"Файл «{name}» является документом PDF, а не таблицей данных. "
            f"Загрузите выгрузку в формате .xlsx, .csv, .json или .xml."
        )

    head_2k_lower = head[:2048].lower()
    if suffix in (".html", ".htm") or any(
        marker in head_2k_lower
        for marker in (b"<!doctype html", b"<html", b"<head>", b"<body")
    ):
        raise DataValidationError(
            f"Файл «{name}» является сохранённой веб-страницей (HTML), а не файлом данных. "
            f"Скачайте сам файл выгрузки (.xlsx, .csv, .json или .xml), а не веб-страницу."
        )

    is_pk_magic = head.startswith((b"PK\x03\x04", b"PK\x05\x06"))
    if is_pk_magic or suffix in (".zip", ".xlsx"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = zf.namelist()
        except (zipfile.BadZipFile, EOFError, zlib.error, OSError) as exc:
            raise DataValidationError(
                f"Файл «{name}» повреждён или скачан не полностью "
                f"(ошибка чтения ZIP-контейнера XLSX). Проверьте размер файла и скачайте его заново."
            ) from exc

        sheet_names = [
            n
            for n in names
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        ]
        if sheet_names:
            return "xlsx"

        inner_datasets = [
            Path(n).name
            for n in names
            if not n.endswith("/") and Path(n).suffix.lower() in SUPPORTED_SOURCE_EXTS
        ]
        if inner_datasets:
            listed = ", ".join(inner_datasets[:5])
            raise DataValidationError(
                f"Файл «{name}» — это нераспакованный ZIP-архив (внутри: {listed}). "
                f"Распакуйте архив и импортируйте файл данных напрямую."
            )
        if suffix == ".xlsx" and "xl/workbook.xml" in names:
            raise DataValidationError(
                f"В файле XLSX «{name}» не найдено ни одного листа с данными."
            )
        raise DataValidationError(
            f"Файл «{name}» является ZIP-архивом (или документом Office), "
            f"но не содержит таблиц Excel (.xlsx)."
        )

    if suffix == ".json":
        return "json"
    if suffix == ".csv":
        return "csv"
    if suffix == ".xml":
        return "xml"

    head_stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if head_stripped.startswith((b"[", b"{")):
        return "json"
    if head_stripped.startswith(b"<"):
        return "xml"
    if (
        b"PlaceNumber" in head
        or b"global_id" in head
        or "Кладбище".encode("utf-8") in head
        or "Кладбище".encode("cp1251") in head
    ):
        return "csv"

    raise DataValidationError(
        f"Не удалось определить формат файла «{name}». "
        f"Поддерживаются файлы выгрузок .xlsx, .csv, .json и .xml."
    )


def _is_valid_source_date(date_str: str) -> bool:
    """Проверяет, что строка YYYY-MM-DD является реальной датой (не заглушкой 2999-01-01).

    Args:
        date_str: Строка даты в формате 'YYYY-MM-DD'.

    Returns:
        True, если дата валидна и год находится в допустимом диапазоне.
    """
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        max_year = datetime.now(timezone.utc).year + 1
        return 1990 <= dt.year <= max_year
    except ValueError:
        return False


def _strip_xml_ns(tag: str) -> str:
    """Удаляет пространство имён XML из имени тега.

    Args:
        tag: Полное имя XML-тега (возможно, в формате '{namespace}localname').

    Returns:
        Локальное имя тега без пространства имён.
    """
    return tag.split("}", 1)[1] if "}" in tag else tag


def _detect_file_encoding(path: Path, sample_size: int = 65536) -> str:
    """Определяет кодировку текстового файла по начальному блоку байт.

    Args:
        path: Путь к проверяемому файлу.
        sample_size: Размер читаемого образца в байтах (по умолчанию 64 КБ).

    Returns:
        Название подходящей кодировки ('utf-16', 'utf-8-sig', 'utf-8' или 'cp1251').
    """
    with path.open("rb") as bf:
        sample = bf.read(sample_size)
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            sample.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _build_plot_record(
    fields: dict[str, Any],
    inscriptions: list[str] | None = None,
) -> PlotRecord | None:
    """Создаёт PlotRecord из сырого словаря атрибутов участка табличной/JSON/XML выгрузки.

    Args:
        fields: Словарь сырых полей участка (global_id, PlaceNumber, Cemetery и др.).
        inscriptions: Предварительно разобранный список строк надписей (если уже извлечён).

    Returns:
        Объект PlotRecord или None, если global_id отсутствует или некорректен.
    """
    raw_gid = fields.get("global_id")
    if raw_gid is None:
        return None
    gid_str = str(raw_gid).strip()
    if not gid_str or gid_str == "global_id":
        return None
    try:
        global_id = int(gid_str)
    except (ValueError, TypeError):
        return None

    place_raw = fields.get("PlaceNumber")
    place_number = str(place_raw).strip() if place_raw is not None else ""
    place_number = place_number or None

    cemetery_raw = fields.get("Cemetery")
    cemetery_name = (
        str(cemetery_raw).strip() if cemetery_raw is not None else ""
    ) or "Неизвестное"

    grave_type = parse_grave_type(fields.get("GraveType"))
    if inscriptions is None:
        raw_insc = fields.get("Inscription")
        inscriptions = (
            split_inscription_cell(str(raw_insc)) if raw_insc is not None else []
        )

    has_tombstone = parse_has_tombstone(fields.get("HasTombstone"), bool(inscriptions))
    cemetery_code, plot_section = parse_place_number(place_number)
    lon, lat, area = extract_geo_info(
        fields.get("geoData"), fields.get("geodata_center")
    )

    return PlotRecord(
        global_id=global_id,
        cemetery_name=cemetery_name,
        cemetery_code=cemetery_code,
        place_number=place_number,
        plot_section=plot_section,
        grave_type=grave_type,
        has_tombstone=has_tombstone,
        inscriptions=inscriptions,
        lon=lon,
        lat=lat,
        area=area,
    )


def detect_source_date(file_path: str | Path) -> str | None:
    """Определяет дату актуальности файла данных из метаданных XLSX или имени файла.

    Приоритеты:
      1. Внутренние метаданные docProps/core.xml (<dcterms:created> / <dcterms:modified>).
      2. Дата упаковки листа xl/worksheets/sheet1.xml внутри ZIP-архива XLSX.
      3. Явная дата в имени файла (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД, исключая заглушку 2999-01-01).

    Args:
        file_path: Путь к файлу источника.

    Returns:
        Строка даты в формате 'YYYY-MM-DD' или None, если дата не указана.
    """
    path = Path(file_path)
    if path.is_file() and (path.suffix.lower() == ".xlsx" or zipfile.is_zipfile(path)):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                if "docProps/core.xml" in zf.namelist():
                    core_xml = zf.read("docProps/core.xml").decode(
                        "utf-8", errors="ignore"
                    )
                    root = ET.fromstring(core_xml)
                    for elem in root.iter():
                        if _strip_xml_ns(elem.tag) in ("created", "modified"):
                            text = (elem.text or "").strip()
                            m = ISO_DATE_PREFIX_RE.match(text)
                            if m and _is_valid_source_date(m.group(1)):
                                return m.group(1)
                for info in zf.infolist():
                    if info.filename.startswith("xl/worksheets/sheet"):
                        y, mo, d = info.date_time[:3]
                        candidate = f"{y:04d}-{mo:02d}-{d:02d}"
                        if _is_valid_source_date(candidate):
                            return candidate
        except (zipfile.BadZipFile, ET.ParseError, OSError):
            pass

    fname = path.name
    for m_dmy in FILENAME_DMY_RE.finditer(fname):
        d_s, m_s, y_s = m_dmy.group(1), m_dmy.group(2), m_dmy.group(3)
        candidate = f"{y_s}-{m_s}-{d_s}"
        if _is_valid_source_date(candidate):
            return candidate

    for m_ymd in FILENAME_YMD_RE.finditer(fname):
        y_s, m_s, d_s = m_ymd.group(1), m_ymd.group(2), m_ymd.group(3)
        candidate = f"{y_s}-{m_s}-{d_s}"
        if _is_valid_source_date(candidate):
            return candidate

    return None


def compute_file_sha256(
    file_path: str | Path, cancel_token: ImportCancelToken | None = None
) -> str:
    """Потоково вычисляет SHA-256 файла блоками по 256 КБ без нагрузки на RAM.

    Args:
        file_path: Путь к файлу.
        cancel_token: Необязательный признак отмены, проверяется после каждого блока.

    Returns:
        Шестнадцатеричная строка SHA-256 дайджеста.

    Raises:
        ImportCancelledError: Отмена запрошена во время вычисления.
    """
    h = hashlib.sha256()
    with Path(file_path).open("rb") as f:
        while chunk := f.read(262144):
            h.update(chunk)
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
    return h.hexdigest()


def _col_letter(cell_ref: str) -> str:
    """Извлекает буквенный индекс колонки из ссылки ячейки XLSX (например, 'A3' -> 'A').

    Args:
        cell_ref: Ссылка на ячейку XLSX (например, 'A3', 'BC12').

    Returns:
        Буквенное обозначение колонки или пустая строка.
    """
    i = 0
    n = len(cell_ref)
    while i < n and cell_ref[i].isalpha():
        i += 1
    return cell_ref[:i]


def _load_xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """Загружает таблицу общих строк xl/sharedStrings.xml, если она не пуста.

    Args:
        zf: Открытый ZIP-архив файла XLSX.

    Returns:
        Список строк из таблицы sharedStrings.
    """
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    strings: list[str] = []
    with zf.open("xl/sharedStrings.xml") as f:
        for _, elem in ET.iterparse(f, events=("end",)):
            if _strip_xml_ns(elem.tag) == "si":
                parts = [
                    t.text or "" for t in elem.iter() if _strip_xml_ns(t.tag) == "t"
                ]
                strings.append("".join(parts))
                elem.clear()
    return strings


def _extract_cell_text(cell_elem: ET.Element, shared_strings: list[str]) -> str:
    """Извлекает текстовое значение одной ячейки <c> из листа XLSX.

    Args:
        cell_elem: XML-элемент ячейки <c>.
        shared_strings: Таблица общих строк XLSX.

    Returns:
        Строковое значение ячейки.
    """
    cell_type = cell_elem.attrib.get("t", "")
    if cell_type == "inlineStr":
        texts: list[str] = []
        for sub in cell_elem.iter():
            if _strip_xml_ns(sub.tag) == "t" and sub.text:
                texts.append(sub.text)
        return "".join(texts)

    val_text = ""
    for sub in cell_elem:
        if _strip_xml_ns(sub.tag) == "v":
            val_text = sub.text or ""
            break

    if cell_type == "s" and val_text:
        idx = int(val_text)
        if 0 <= idx < len(shared_strings):
            return shared_strings[idx]
    return val_text


def iter_xlsx_plots(
    file_path: str | Path,
    display_name: str | None = None,
) -> Iterator[PlotRecord]:
    """Потоково читает записи участков из оригинального файла .xlsx (data.mos.ru).

    Игнорирует некорректный тег <dimension ref="A1"/> и очищает обработанные XML-узлы
    для поддержания константного потребления оперативной памяти.

    Args:
        file_path: Путь к файлу .xlsx.
        display_name: Отображаемое имя файла для сообщений об ошибках.

    Yields:
        Объекты PlotRecord по одному на строку участка.

    Raises:
        DataValidationError: Если файл не является корректной таблицей XLSX реестра захоронений.
    """
    path = Path(file_path)
    name = (display_name or path.name).strip() or path.name
    fmt = validate_source_file(path, display_name=name)
    if fmt != "xlsx":
        raise DataValidationError(f"Файл «{name}» не является таблицей Excel (.xlsx).")

    plots_yielded = 0
    try:
        with zipfile.ZipFile(path, "r") as zf:
            shared_strings = _load_xlsx_shared_strings(zf)
            sheet_names = [
                n
                for n in zf.namelist()
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            ]
            if not sheet_names:
                raise DataValidationError(
                    f"В файле XLSX «{name}» не найдено ни одного листа с данными."
                )
            sheet_path = sorted(sheet_names)[0]

            header_map: dict[str, str] = {}
            first_row_cols: list[str] = []
            non_empty_rows_seen = 0
            sheet_data_elem: ET.Element | None = None
            rows_since_purge = 0

            with zf.open(sheet_path) as f:
                for event, elem in ET.iterparse(f, events=("start", "end")):
                    tag_name = _strip_xml_ns(elem.tag)
                    if event == "start":
                        if tag_name == "sheetData":
                            sheet_data_elem = elem
                        continue

                    if tag_name != "row":
                        continue

                    row_cells: dict[str, str] = {}
                    col_idx = 0
                    for cell in elem:
                        if _strip_xml_ns(cell.tag) == "c":
                            ref = cell.attrib.get("r", "")
                            col = _col_letter(ref) or (
                                chr(ord("A") + col_idx)
                                if col_idx < 26
                                else str(col_idx)
                            )
                            row_cells[col] = _extract_cell_text(cell, shared_strings)
                            col_idx += 1

                    elem.clear()
                    rows_since_purge += 1
                    if sheet_data_elem is not None and rows_since_purge >= 250:
                        del sheet_data_elem[:]
                        rows_since_purge = 0

                    if not row_cells or not any(
                        v and v.strip() for v in row_cells.values()
                    ):
                        continue

                    if not header_map:
                        non_empty_rows_seen += 1
                        candidate_map = {
                            col: _normalize_header_name(val)
                            for col, val in row_cells.items()
                        }
                        if "global_id" in candidate_map.values():
                            header_map = candidate_map
                            continue
                        if non_empty_rows_seen == 1:
                            first_row_cols = [
                                val.strip()
                                for val in row_cells.values()
                                if val and val.strip()
                            ]
                            continue
                        found_cols = (
                            ", ".join(first_row_cols[:6]) if first_row_cols else "пусто"
                        )
                        raise DataValidationError(
                            f"Таблица XLSX «{name}» не похожа на выгрузку реестра захоронений data.mos.ru "
                            f"(отсутствует обязательная колонка «global_id»). Обнаружены колонки: {found_cols}."
                        )

                    field_vals = {
                        header_map.get(col, col): val for col, val in row_cells.items()
                    }
                    record = _build_plot_record(field_vals)
                    if record is not None:
                        plots_yielded += 1
                        yield record

            if not header_map:
                if first_row_cols:
                    found_cols = ", ".join(first_row_cols[:6])
                    raise DataValidationError(
                        f"Таблица XLSX «{name}» не похожа на выгрузку реестра захоронений data.mos.ru "
                        f"(отсутствует обязательная колонка «global_id»). Обнаружены колонки: {found_cols}."
                    )
                raise DataValidationError(
                    f"В файле XLSX «{name}» не найдено ни одной строки с данными."
                )
    except DataValidationError:
        raise
    except (ET.ParseError, zipfile.BadZipFile, EOFError, zlib.error, OSError) as exc:
        raise DataValidationError(
            f"Файл XLSX «{name}» повреждён или обрезан "
            f"(ошибка разбора XML на участке №{plots_yielded + 1}): {exc}"
        ) from exc


def _iter_json_objects_from_file(
    file_path: str | Path,
    display_name: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Потоково извлекает JSON-объекты верхнего уровня из массива JSON или JSONL.

    Использует быстрый построчный разбор однострочных объектов через json.loads
    с автоматическим откатом на потоковый JSONDecoder.raw_decode для многострочного JSON.
    Проверяет целостность JSON-документа и выбрасывает DataValidationError при обрыве файла.

    Args:
        file_path: Путь к JSON-файлу.
        display_name: Отображаемое имя файла для сообщений об ошибках.

    Yields:
        Словари объектов верхнего уровня.

    Raises:
        DataValidationError: Если JSON повреждён, обрезан или содержит бинарные данные.
    """
    path = Path(file_path)
    name = (display_name or path.name).strip() or path.name
    validate_source_file(path, display_name=name)

    with path.open("rb") as bf:
        sample = bf.read(65536)
    if b"\x00" in sample:
        raise DataValidationError(
            f"Файл «{name}» имеет неподдерживаемую кодировку или содержит бинарные данные "
            f"(ожидался текстовый JSON в UTF-8 или Windows-1251)."
        )

    chosen_enc = _detect_file_encoding(path, sample_size=65536)
    decoder = json.JSONDecoder()
    buffer = ""
    first_char: str | None = None
    last_non_ws_char: str = ""
    objs_yielded = 0

    try:
        with path.open("r", encoding=chosen_enc) as f:
            for raw_line in f:
                if "\x00" in raw_line:
                    raise DataValidationError(
                        f"Файл «{name}» содержит бинарные данные (нулевые байты) "
                        f"и не является корректным JSON-документом."
                    )
                stripped = raw_line.strip().lstrip("\ufeff").strip()
                if stripped:
                    if first_char is None:
                        first_char = stripped[0]
                        if first_char not in ("[", "{"):
                            raise DataValidationError(
                                f"Файл «{name}» не является корректным JSON-документом."
                            )
                    last_non_ws_char = stripped[-1]

                if not buffer:
                    if not stripped or stripped in ("[", "]", ","):
                        continue
                    candidate = (
                        stripped[:-1].rstrip() if stripped.endswith(",") else stripped
                    )
                    if candidate.startswith("{") and candidate.endswith("}"):
                        try:
                            obj = json.loads(candidate)
                            if isinstance(obj, dict):
                                objs_yielded += 1
                                yield obj
                                continue
                        except json.JSONDecodeError:
                            pass

                buffer += raw_line
                if len(buffer) > 16 * 1024 * 1024:
                    raise DataValidationError(
                        f"Файл JSON «{name}» содержит синтаксическую ошибку или повреждён."
                    )
                idx = 0
                buf_len = len(buffer)
                while idx < buf_len:
                    while idx < buf_len and buffer[idx] in " \t\r\n[,":
                        idx += 1
                    if idx >= buf_len or buffer[idx] == "]":
                        break
                    try:
                        obj, next_idx = decoder.raw_decode(buffer, idx)
                        if not isinstance(obj, dict):
                            raise DataValidationError(
                                f"JSON-файл «{name}» имеет неизвестную структуру и не содержит "
                                f"записей захоронений data.mos.ru (ожидались JSON-объекты)."
                            )
                        objs_yielded += 1
                        yield obj
                        idx = next_idx
                    except json.JSONDecodeError:
                        break
                buffer = buffer[idx:]
    except UnicodeDecodeError as exc:
        raise DataValidationError(
            f"Файл «{name}» имеет неподдерживаемую кодировку или содержит бинарные данные "
            f"(ожидался текстовый JSON в UTF-8 или Windows-1251)."
        ) from exc

    tail = buffer.strip()
    if tail not in ("", "]") or (first_char == "[" and last_non_ws_char != "]"):
        raise DataValidationError(
            f"Файл JSON «{name}» обрезан, повреждён или скачан не полностью "
            f"(неожиданный конец файла после {objs_yielded} записей)."
        )


def _parse_original_json_object(obj: dict[str, Any]) -> PlotRecord | None:
    """Преобразует объект оригинального JSON data.mos.ru (плоский или с Cells) в PlotRecord.

    Args:
        obj: Словарь одной записи JSON.

    Returns:
        Объект PlotRecord или None, если запись не содержит валидного global_id.
    """
    cells = obj.get("Cells") if isinstance(obj.get("Cells"), dict) else obj
    raw_gid = cells.get("global_id") or obj.get("global_id")
    if raw_gid is None:
        return None

    raw_insc = cells.get("Inscription")
    inscriptions: list[str] = []
    if isinstance(raw_insc, str):
        inscriptions = split_inscription_cell(raw_insc)
    elif isinstance(raw_insc, list):
        for item in raw_insc:
            if isinstance(item, str) and item.strip():
                inscriptions.extend(split_inscription_cell(item))
            elif isinstance(item, dict):
                val = item.get("Inscription")
                if isinstance(val, str) and val.strip():
                    inscriptions.extend(split_inscription_cell(val))

    fields = dict(cells)
    fields["global_id"] = raw_gid
    return _build_plot_record(fields, inscriptions=inscriptions)


def iter_json_plots(
    file_path: str | Path,
    display_name: str | None = None,
) -> Iterator[PlotRecord]:
    """Потоково читает участки из оригинального JSON-файла выгрузки data.mos.ru.

    Поддерживает как прямую выгрузку портала (объекты с полем 'global_id'),
    так и формат API apidata.mos.ru (объекты с вложенным словарём 'Cells').

    Args:
        file_path: Путь к файлу .json.
        display_name: Отображаемое имя файла для сообщений об ошибках.

    Yields:
        Объекты PlotRecord.

    Raises:
        DataValidationError: Если JSON пуст, обрезан или имеет неизвестную схему.
    """
    path = Path(file_path)
    name = (display_name or path.name).strip() or path.name
    objs_seen = 0
    plots_yielded = 0

    for obj in _iter_json_objects_from_file(path, display_name=name):
        objs_seen += 1
        cells_dict = obj.get("Cells") if isinstance(obj.get("Cells"), dict) else None
        is_original_schema = "global_id" in obj or (
            cells_dict is not None and "global_id" in cells_dict
        )

        if not is_original_schema:
            if plots_yielded == 0:
                raise DataValidationError(
                    f"JSON-файл «{name}» имеет неизвестную структуру и не содержит записей "
                    f"захоронений data.mos.ru (ожидалось поле «global_id»)."
                )
            continue

        parsed = _parse_original_json_object(obj)
        if parsed is not None:
            plots_yielded += 1
            yield parsed

    if objs_seen == 0 or plots_yielded == 0:
        raise DataValidationError(
            f"В JSON-файле «{name}» не найдено ни одной записи о захоронениях data.mos.ru."
        )


def iter_csv_plots(
    file_path: str | Path,
    display_name: str | None = None,
) -> Iterator[PlotRecord]:
    """Потоково читает записи участков из оригинального CSV-файла data.mos.ru.

    Автоматически определяет кодировку (utf-16 / utf-8-sig / utf-8 / cp1251),
    разделитель (; или ,) и поддерживает русские заголовки колонок.

    Args:
        file_path: Путь к файлу .csv.
        display_name: Отображаемое имя файла для сообщений об ошибках.

    Yields:
        Объекты PlotRecord.

    Raises:
        DataValidationError: Если файл содержит бинарные данные, некорректную кодировку или не имеет колонки global_id.
    """
    path = Path(file_path)
    name = (display_name or path.name).strip() or path.name
    validate_source_file(path, display_name=name)

    chosen_enc = _detect_file_encoding(path, sample_size=65536)
    if chosen_enc != "utf-16":
        with path.open("rb") as bf:
            if b"\x00" in bf.read(65536):
                raise DataValidationError(
                    f"Файл «{name}» содержит бинарные данные (нулевые байты) "
                    f"и не является текстовым CSV-файлом."
                )

    with path.open("r", encoding=chosen_enc, errors="ignore") as hf:
        head_sample = hf.read(4096)
    semi_count = head_sample.count(";")
    comma_count = head_sample.count(",")
    tab_count = head_sample.count("\t")
    if tab_count > semi_count and tab_count > comma_count:
        delimiter = "\t"
    elif semi_count >= comma_count:
        delimiter = ";"
    else:
        delimiter = ","

    csv.field_size_limit(10 * 1024 * 1024)
    try:
        with path.open("r", encoding=chosen_enc, newline="") as f:
            reader = csv.reader(f, delimiter=delimiter)
            headers: list[str] = []
            first_row_cols: list[str] = []
            non_empty_rows_seen = 0

            for row in reader:
                if not row or not any(c and c.strip() for c in row):
                    continue
                if any("\x00" in c for c in row):
                    raise DataValidationError(
                        f"Файл «{name}» содержит бинарные данные (нулевые байты) "
                        f"и не является текстовым CSV-файлом."
                    )
                if not headers:
                    non_empty_rows_seen += 1
                    candidate_headers = [_normalize_header_name(col) for col in row]
                    if "global_id" in candidate_headers:
                        headers = candidate_headers
                        continue
                    if non_empty_rows_seen == 1:
                        first_row_cols = [
                            col.strip() for col in row if col and col.strip()
                        ]
                        continue
                    found_cols = (
                        ", ".join(first_row_cols[:6]) if first_row_cols else "пусто"
                    )
                    raise DataValidationError(
                        f"CSV-файл «{name}» не содержит обязательной колонки «global_id» "
                        f"(обнаружены колонки: {found_cols}). Проверьте, что это выгрузка реестра захоронений data.mos.ru."
                    )

                row_map = {
                    headers[i]: row[i] for i in range(min(len(headers), len(row)))
                }
                record = _build_plot_record(row_map)
                if record is not None:
                    yield record

            if not headers:
                found_cols = (
                    ", ".join(first_row_cols[:6]) if first_row_cols else "пусто"
                )
                raise DataValidationError(
                    f"CSV-файл «{name}» не содержит обязательной колонки «global_id» "
                    f"(обнаружены колонки: {found_cols}). Проверьте, что это выгрузка реестра захоронений data.mos.ru."
                )
    except DataValidationError:
        raise
    except UnicodeDecodeError as exc:
        raise DataValidationError(
            f"Ошибка декодирования текста в CSV-файле «{name}»: {exc}. "
            f"Сохраните файл в кодировке UTF-8 или Windows-1251."
        ) from exc
    except csv.Error as exc:
        raise DataValidationError(
            f"Ошибка формата CSV в файле «{name}»: {exc}. "
            f"Возможно, файл повреждён или содержит незакрытые кавычки."
        ) from exc


def iter_xml_plots(
    file_path: str | Path,
    display_name: str | None = None,
) -> Iterator[PlotRecord]:
    """Потоково читает записи участков из оригинального XML-файла data.mos.ru.

    Args:
        file_path: Путь к файлу .xml.
        display_name: Отображаемое имя файла для сообщений об ошибках.

    Yields:
        Объекты PlotRecord.

    Raises:
        DataValidationError: Если XML повреждён, обрезан или не содержит записей о захоронениях.
    """
    path = Path(file_path)
    name = (display_name or path.name).strip() or path.name
    validate_source_file(path, display_name=name)

    plots_yielded = 0
    elems_seen = 0
    try:
        for _, elem in ET.iterparse(str(path), events=("end",)):
            elems_seen += 1
            child_tags = {_strip_xml_ns(ch.tag) for ch in elem}
            if "global_id" not in child_tags or "Cemetery" not in child_tags:
                if plots_yielded == 0 and elems_seen >= 5000:
                    raise DataValidationError(
                        f"XML-файл «{name}» не содержит записей о захоронениях data.mos.ru "
                        f"(ожидались элементы с полями «global_id» и «Cemetery»)."
                    )
                continue

            field_map: dict[str, str] = {}
            inscriptions: list[str] = []

            for ch in elem:
                ctag = _strip_xml_ns(ch.tag)
                if ctag == "Inscription":
                    nested = [
                        sub.text.strip()
                        for sub in ch.iter()
                        if sub is not ch
                        and _strip_xml_ns(sub.tag) == "Inscription"
                        and sub.text
                        and sub.text.strip()
                    ]
                    if nested:
                        for item in nested:
                            inscriptions.extend(split_inscription_cell(item))
                    elif ch.text and ch.text.strip():
                        inscriptions.extend(split_inscription_cell(ch.text))
                else:
                    field_map[ctag] = (ch.text or "").strip()

            elem.clear()

            record = _build_plot_record(field_map, inscriptions=inscriptions)
            if record is not None:
                plots_yielded += 1
                yield record
    except DataValidationError:
        raise
    except ET.ParseError as exc:
        raise DataValidationError(
            f"Файл XML «{name}» повреждён, обрезан или содержит синтаксическую ошибку XML: {exc}"
        ) from exc

    if plots_yielded == 0:
        raise DataValidationError(
            f"В XML-файле «{name}» не найдено записей о захоронениях data.mos.ru "
            f"(ожидались элементы с полями «global_id» и «Cemetery»)."
        )


def iter_plots_from_file(
    file_path: str | Path,
    display_name: str | None = None,
) -> Iterator[PlotRecord]:
    """Автоматически определяет формат входного файла и возвращает поток участков.

    Args:
        file_path: Путь к входному файлу (.xlsx, .json, .csv, .xml).
        display_name: Отображаемое имя файла для сообщений об ошибках.

    Returns:
        Итератор объектов PlotRecord.

    Raises:
        DataValidationError: Если файл не найден, пуст, повреждён или его формат не поддерживается.
    """
    path = Path(file_path)
    name = (display_name or path.name).strip() or path.name
    fmt = validate_source_file(path, display_name=name)
    if fmt == "xlsx":
        return iter_xlsx_plots(path, display_name=name)
    if fmt == "json":
        return iter_json_plots(path, display_name=name)
    if fmt == "csv":
        return iter_csv_plots(path, display_name=name)
    if fmt == "xml":
        return iter_xml_plots(path, display_name=name)
    raise DataValidationError(f"Не удалось определить формат файла «{name}».")


def _ensure_cemetery(
    conn: sqlite3.Connection,
    cemetery_cache: dict[str, tuple[int, str | None]],
    name: str,
    code: str | None,
) -> int:
    """Возвращает id кладбища из кэша или создаёт/обновляет запись в таблице cemeteries.

    Args:
        conn: Открытое соединение SQLite.
        cemetery_cache: Кэш кладбищ {name: (id, code)}.
        name: Наименование кладбища.
        code: Трёхзначный код кладбища из PlaceNumber (если есть).

    Returns:
        Числовой идентификатор кладбища в таблице cemeteries.
    """
    cached = cemetery_cache.get(name)
    if cached is not None:
        cid, existing_code = cached
        if code and not existing_code:
            conn.execute(
                "UPDATE cemeteries SET code = ? WHERE id = ? AND code IS NULL",
                (code, cid),
            )
            cemetery_cache[name] = (cid, code)
        return cid

    conn.execute(
        """
        INSERT INTO cemeteries (name, code)
        VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET
            code = COALESCE(excluded.code, cemeteries.code)
        """,
        (name, code),
    )
    row = conn.execute(
        "SELECT id, code FROM cemeteries WHERE name = ?", (name,)
    ).fetchone()
    cid, db_code = int(row[0]), row[1]
    cemetery_cache[name] = (cid, db_code)
    return cid


def _person_tuple(global_id: int, raw_line: str) -> tuple[Any, ...]:
    """Разбирает строку надписи и возвращает кортеж для вставки в таблицу persons.

    Args:
        global_id: Идентификатор участка захоронения.
        raw_line: Исходная строка надписи.

    Returns:
        Кортеж из 12 значений для SQL-запроса INSERT INTO persons.
    """
    p = parse_inscription_line(raw_line)
    return (
        global_id,
        p.raw_line,
        p.surname,
        p.firstname,
        p.patronymic,
        p.fio_norm,
        p.birth_year,
        p.birth_date_raw,
        p.death_year,
        p.death_date_raw,
        p.single_date_year,
        p.single_date_raw,
    )


def _norm_inscription_list(lines: list[str]) -> list[str]:
    """Нормализует список строк надписей (нижний регистр, ё -> е) для сравнения без учёта регистра.

    Args:
        lines: Список строк надписей.

    Returns:
        Список нормализованных строк.
    """
    return [s.lower().replace("ё", "е") for s in lines]


def _flush_batch(
    conn: sqlite3.Connection,
    batch: list[PlotRecord],
    cemetery_cache: dict[str, tuple[int, str | None]],
    check_existing_in_db: bool,
    source_date: str | None = None,
) -> tuple[int, int, int, int]:
    """Записывает пачку участков и надписей в SQLite по правилам 'Свежесть + Полнота'.

    Args:
        conn: Открытое соединение SQLite.
        batch: Пачка записей PlotRecord для записи.
        cemetery_cache: Кэш кладбищ {name: (id, code)}.
        check_existing_in_db: Проверять ли наличие существующих участков в БД перед записью.
        source_date: Дата актуальности импортируемого файла ('YYYY-MM-DD' или None).

    Returns:
        Кортеж (plots_inserted, plots_updated, persons_inserted, persons_seen_in_batch).
    """
    if not batch:
        return 0, 0, 0, 0

    # Слияние возможных дублей global_id внутри одной пачки
    by_gid: dict[int, PlotRecord] = {}
    for item in batch:
        existing = by_gid.get(item.global_id)
        if existing is None:
            by_gid[item.global_id] = item
        else:
            existing.cemetery_code = existing.cemetery_code or item.cemetery_code
            existing.place_number = existing.place_number or item.place_number
            existing.plot_section = existing.plot_section or item.plot_section
            existing.has_tombstone = max(existing.has_tombstone, item.has_tombstone)
            existing.lon = existing.lon if existing.lon is not None else item.lon
            existing.lat = existing.lat if existing.lat is not None else item.lat
            existing.area = existing.area if existing.area is not None else item.area
            existing.inscriptions.extend(item.inscriptions)

    existing_plots: dict[int, dict[str, Any]] = {}
    existing_lines_by_gid: dict[int, list[str]] = {}

    if check_existing_in_db:
        gids = list(by_gid.keys())
        placeholders = ",".join("?" for _ in gids)
        rows = conn.execute(
            f"""
            SELECT
                global_id, cemetery_id, place_number, plot_section,
                grave_type, has_tombstone, person_count, lon, lat, area, source_date
            FROM plots
            WHERE global_id IN ({placeholders})
            """,
            gids,
        ).fetchall()
        for r in rows:
            existing_plots[int(r[0])] = {
                "cemetery_id": int(r[1]),
                "place_number": r[2],
                "plot_section": r[3],
                "grave_type": int(r[4]),
                "has_tombstone": int(r[5]),
                "person_count": int(r[6]),
                "lon": float(r[7]) if r[7] is not None else None,
                "lat": float(r[8]) if r[8] is not None else None,
                "area": float(r[9]) if r[9] is not None else None,
                "source_date": r[10],
            }

        if existing_plots:
            ex_list = list(existing_plots.keys())
            ex_ph = ",".join("?" for _ in ex_list)
            p_rows = conn.execute(
                f"SELECT global_id, raw_line FROM persons "
                f"WHERE global_id IN ({ex_ph}) ORDER BY id",
                ex_list,
            ).fetchall()
            for r in p_rows:
                existing_lines_by_gid.setdefault(int(r[0]), []).append(r[1])

    plots_to_insert: list[tuple[Any, ...]] = []
    plots_to_upsert: list[tuple[Any, ...]] = []
    gids_to_clear_persons: list[tuple[int]] = []
    persons_to_insert: list[tuple[Any, ...]] = []

    inserted_count = 0
    updated_count = 0
    persons_seen_in_batch = 0

    for gid, plot in by_gid.items():
        cid = _ensure_cemetery(
            conn, cemetery_cache, plot.cemetery_name, plot.cemetery_code
        )
        dedup_new = deduplicate_inscriptions(plot.inscriptions)
        persons_seen_in_batch += len(dedup_new)
        old_plot = existing_plots.get(gid)

        if old_plot is None:
            inserted_count += 1
            has_tomb = 1 if (plot.has_tombstone or dedup_new) else 0
            plots_to_insert.append(
                (
                    gid,
                    cid,
                    plot.place_number,
                    plot.plot_section,
                    plot.grave_type,
                    has_tomb,
                    len(dedup_new),
                    plot.lon,
                    plot.lat,
                    plot.area,
                    source_date,
                )
            )
            for line in dedup_new:
                persons_to_insert.append(_person_tuple(gid, line))
        else:
            old_date: str | None = old_plot["source_date"]
            old_lines = existing_lines_by_gid.get(gid, [])

            new_is_newer = source_date is not None and (
                old_date is None or source_date > old_date
            )
            new_is_older = old_date is not None and (
                source_date is None or source_date < old_date
            )

            if new_is_older:
                # Новый источник старше (или без даты при наличии датированной записи в БД):
                # дополняет только пустые (NULL) поля и не перезаписывает надписи
                merged_cid = old_plot["cemetery_id"]
                merged_place = (
                    old_plot["place_number"]
                    if old_plot["place_number"] is not None
                    else plot.place_number
                )
                merged_sec = (
                    old_plot["plot_section"]
                    if old_plot["plot_section"] is not None
                    else plot.plot_section
                )
                merged_gtype = old_plot["grave_type"] or plot.grave_type
                merged_lon = (
                    old_plot["lon"] if old_plot["lon"] is not None else plot.lon
                )
                merged_lat = (
                    old_plot["lat"] if old_plot["lat"] is not None else plot.lat
                )
                merged_area = (
                    old_plot["area"] if old_plot["area"] is not None else plot.area
                )
                merged_date = old_date
            else:
                # Новый источник свежее или равной даты актуальности:
                # его непустые атрибуты имеют приоритет
                merged_cid = (
                    cid
                    if plot.cemetery_name != "Неизвестное"
                    else old_plot["cemetery_id"]
                )
                merged_place = (
                    plot.place_number
                    if plot.place_number is not None
                    else old_plot["place_number"]
                )
                merged_sec = (
                    plot.plot_section
                    if plot.plot_section is not None
                    else old_plot["plot_section"]
                )
                merged_gtype = plot.grave_type or old_plot["grave_type"]
                merged_lon = plot.lon if plot.lon is not None else old_plot["lon"]
                merged_lat = plot.lat if plot.lat is not None else old_plot["lat"]
                merged_area = plot.area if plot.area is not None else old_plot["area"]
                merged_date = source_date if new_is_newer else (source_date or old_date)

            if new_is_newer:
                if dedup_new:
                    final_lines = dedup_new
                    need_replace_persons = _norm_inscription_list(
                        old_lines
                    ) != _norm_inscription_list(dedup_new)
                else:
                    final_lines = old_lines
                    need_replace_persons = False
            elif new_is_older:
                if old_lines:
                    final_lines = old_lines
                    need_replace_persons = False
                elif dedup_new:
                    final_lines = dedup_new
                    need_replace_persons = True
                else:
                    final_lines = []
                    need_replace_persons = False
            else:
                if not dedup_new:
                    final_lines = old_lines
                    need_replace_persons = False
                elif not old_lines:
                    final_lines = dedup_new
                    need_replace_persons = True
                else:
                    old_norms = _norm_inscription_list(old_lines)
                    new_norms = _norm_inscription_list(dedup_new)
                    if old_norms == new_norms:
                        final_lines = old_lines
                        need_replace_persons = False
                    else:
                        final_lines = deduplicate_inscriptions(old_lines + dedup_new)
                        need_replace_persons = (
                            _norm_inscription_list(final_lines) != old_norms
                        )

            has_tomb = (
                1
                if (plot.has_tombstone or old_plot["has_tombstone"] or final_lines)
                else 0
            )
            plot_changed = (
                merged_cid != old_plot["cemetery_id"]
                or merged_place != old_plot["place_number"]
                or merged_sec != old_plot["plot_section"]
                or merged_gtype != old_plot["grave_type"]
                or has_tomb != old_plot["has_tombstone"]
                or len(final_lines) != old_plot["person_count"]
                or merged_lon != old_plot["lon"]
                or merged_lat != old_plot["lat"]
                or merged_area != old_plot["area"]
                or merged_date != old_plot["source_date"]
            )
            if plot_changed or need_replace_persons:
                updated_count += 1
                if plot_changed:
                    plots_to_upsert.append(
                        (
                            gid,
                            merged_cid,
                            merged_place,
                            merged_sec,
                            merged_gtype,
                            has_tomb,
                            len(final_lines),
                            merged_lon,
                            merged_lat,
                            merged_area,
                            merged_date,
                        )
                    )
                if need_replace_persons:
                    gids_to_clear_persons.append((gid,))
                    for line in final_lines:
                        persons_to_insert.append(_person_tuple(gid, line))

    if plots_to_insert:
        conn.executemany(
            """
            INSERT INTO plots (
                global_id, cemetery_id, place_number, plot_section,
                grave_type, has_tombstone, person_count, lon, lat, area, source_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            plots_to_insert,
        )

    if plots_to_upsert:
        conn.executemany(
            """
            INSERT INTO plots (
                global_id, cemetery_id, place_number, plot_section,
                grave_type, has_tombstone, person_count, lon, lat, area, source_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(global_id) DO UPDATE SET
                cemetery_id = excluded.cemetery_id,
                place_number = excluded.place_number,
                plot_section = excluded.plot_section,
                grave_type = excluded.grave_type,
                has_tombstone = excluded.has_tombstone,
                person_count = excluded.person_count,
                lon = excluded.lon,
                lat = excluded.lat,
                area = excluded.area,
                source_date = excluded.source_date
            """,
            plots_to_upsert,
        )

    if gids_to_clear_persons:
        conn.executemany(
            "DELETE FROM persons WHERE global_id = ?", gids_to_clear_persons
        )

    if persons_to_insert:
        conn.executemany(
            """
            INSERT INTO persons (
                global_id, raw_line, surname, firstname, patronymic, fio_norm,
                birth_year, birth_date_raw, death_year, death_date_raw,
                single_date_year, single_date_raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            persons_to_insert,
        )

    return inserted_count, updated_count, len(persons_to_insert), persons_seen_in_batch


def import_file(
    file_path: str | Path,
    db_path: str | Path,
    source_date: str | None = None,
    file_sha256: str | None = None,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
    cancel_token: ImportCancelToken | None = None,
) -> ImportStats:
    """Выполняет потоковый импорт файла выгрузки в базу данных SQLite с перестроением FTS5.

    Весь импорт (данные, FTS-индексы, запись в import_history) выполняется одной
    транзакцией: при ошибке или отмене база остаётся ровно такой, какой была до импорта.

    Оптимизировано для компьютеров с малым объёмом RAM: читает файл построчно/потоково,
    сбрасывает транзакции небольшими порциями и уступает GIL для плавной работы интерфейса.

    Args:
        file_path: Путь к импортируемому файлу (.xlsx, .json, .csv, .xml).
        db_path: Путь к файлу базы данных SQLite.
        source_date: Явная дата актуальности ('YYYY-MM-DD') или None (определить автоматически).
        file_sha256: Предвычисленный SHA-256 файла (если уже известен).
        progress_cb: Необязательный колбэк для отправки состояния прогресса.
        cancel_token: Необязательный признак отмены (ImportCancelToken.cancel из другого потока).

    Returns:
        Статистика выполненного импорта (ImportStats).

    Raises:
        ImportCancelledError: Импорт остановлен пользователем; изменения откатаны.
    """
    t0 = time.perf_counter()
    src_path = Path(file_path)
    validate_source_file(src_path)
    file_size = src_path.stat().st_size
    effective_date = (
        source_date
        if (source_date and _is_valid_source_date(source_date))
        else detect_source_date(src_path)
    )

    plots_seen = 0
    persons_seen = 0
    plots_inserted = 0
    plots_updated = 0
    persons_inserted = 0

    def _notify_progress(phase: str, **extra: Any) -> None:
        if progress_cb is not None:
            progress_cb(
                {
                    "phase": phase,
                    "file_name": src_path.name,
                    "source_date": effective_date,
                    "plots_seen": plots_seen,
                    "persons_seen": persons_seen,
                    "plots_inserted": plots_inserted,
                    "plots_updated": plots_updated,
                    "persons_inserted": persons_inserted,
                    "elapsed_sec": time.perf_counter() - t0,
                    **extra,
                }
            )

    _notify_progress("hashing", elapsed_sec=0.0)

    sha256_hex = file_sha256 or compute_file_sha256(src_path, cancel_token)
    if cancel_token is not None:
        cancel_token.raise_if_cancelled()
    logger.info(
        "Начало импорта файла %s (дата данных: %s, sha256: %s...) в БД %s",
        src_path.name,
        effective_date or "не указана",
        sha256_hex[:12],
        db_path,
    )

    with open_import_db(db_path) as conn, _cancellable(conn, cancel_token):
        initial_plots = int(conn.execute("SELECT COUNT(*) FROM plots").fetchone()[0])
        check_existing = initial_plots > 0

        cemetery_cache: dict[str, tuple[int, str | None]] = {}
        for row in conn.execute("SELECT id, name, code FROM cemeteries"):
            cemetery_cache[row[1]] = (int(row[0]), row[2])

        seen_in_this_file: set[int] = set()
        batch: list[PlotRecord] = []

        _notify_progress("reading")

        for plot in iter_plots_from_file(src_path):
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()
            plots_seen += 1
            if plot.global_id in seen_in_this_file:
                check_existing = True
            else:
                seen_in_this_file.add(plot.global_id)

            batch.append(plot)
            if len(batch) >= BATCH_SIZE:
                ins, upd, p_ins, p_seen = _flush_batch(
                    conn, batch, cemetery_cache, check_existing, effective_date
                )
                plots_inserted += ins
                plots_updated += upd
                persons_inserted += p_ins
                persons_seen += p_seen
                batch.clear()

                elapsed = time.perf_counter() - t0
                _notify_progress("reading", elapsed_sec=elapsed)

                if plots_seen % 50000 == 0:
                    logger.info(
                        "Обработано участков: %d (захоронений: %d, новых уч.: %d, обновлено: %d, %.1f уч/с)",
                        plots_seen,
                        persons_seen,
                        plots_inserted,
                        plots_updated,
                        plots_seen / max(elapsed, 0.001),
                    )
                if progress_cb is not None:
                    time.sleep(0)

        if batch:
            ins, upd, p_ins, p_seen = _flush_batch(
                conn, batch, cemetery_cache, check_existing, effective_date
            )
            plots_inserted += ins
            plots_updated += upd
            persons_inserted += p_ins
            persons_seen += p_seen
            batch.clear()

        if plots_seen == 0:
            raise DataValidationError(
                f"В файле «{src_path.name}» не найдено ни одной записи о захоронениях "
                f"(проверьте, что файл является выгрузкой набора данных data.mos.ru/opendata/64023)."
            )

        # Без commit: данные, индексы и история фиксируются одной транзакцией в конце,
        # чтобы остановка на любом этапе полностью откатывала импорт.
        _notify_progress("indexing")
        logger.info(
            "Перестроение полнотекстовых индексов FTS5 (unicode61 + trigram)..."
        )

        def _on_index_step(step: int, total: int, label: str) -> None:
            logger.info("Индексация, шаг %d из %d: %s", step, total, label)
            _notify_progress(
                "indexing", index_step=step, index_steps=total, index_label=label
            )
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()

        rebuild_fts_indexes(conn, on_step=_on_index_step)

        imported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """
            INSERT INTO import_history (
                sha256, file_name, file_size, source_date,
                plots_seen, persons_seen, plots_inserted, plots_updated, persons_inserted, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                file_name = excluded.file_name,
                file_size = excluded.file_size,
                source_date = COALESCE(excluded.source_date, import_history.source_date),
                plots_seen = excluded.plots_seen,
                persons_seen = excluded.persons_seen,
                plots_inserted = excluded.plots_inserted,
                plots_updated = excluded.plots_updated,
                persons_inserted = excluded.persons_inserted,
                imported_at = excluded.imported_at
            """,
            (
                sha256_hex,
                src_path.name,
                file_size,
                effective_date,
                plots_seen,
                persons_seen,
                plots_inserted,
                plots_updated,
                persons_inserted,
                imported_at,
            ),
        )

        total_plots = int(conn.execute("SELECT COUNT(*) FROM plots").fetchone()[0])
        total_persons = int(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0])

        # Последняя точка, где остановка ещё возможна; после commit импорт считается завершённым.
        if cancel_token is not None:
            cancel_token.raise_if_cancelled()
        conn.commit()

    elapsed_sec = time.perf_counter() - t0
    _notify_progress(
        "done",
        total_plots_in_db=total_plots,
        total_persons_in_db=total_persons,
        elapsed_sec=elapsed_sec,
    )
    logger.info(
        "Импорт завершён за %.2f с. Всего в БД: %d участков, %d надписей.",
        elapsed_sec,
        total_plots,
        total_persons,
    )
    return ImportStats(
        file_path=str(src_path),
        plots_seen=plots_seen,
        plots_inserted=plots_inserted,
        plots_updated=plots_updated,
        persons_inserted=persons_inserted,
        total_plots_in_db=total_plots,
        total_persons_in_db=total_persons,
        elapsed_sec=elapsed_sec,
        source_date=effective_date,
        sha256=sha256_hex,
        persons_seen=persons_seen,
    )


@functools.lru_cache(maxsize=64)
def _compute_cached_sha256(path_str: str, size: int, mtime_ns: int) -> str:
    """Вычисляет SHA-256 файла с мемоизацией по (путь, размер, mtime_ns).

    Args:
        path_str: Абсолютный путь к файлу в виде строки.
        size: Размер файла в байтах.
        mtime_ns: Время модификации файла в наносекундах.

    Returns:
        Шестнадцатеричная строка SHA-256 дайджеста.
    """
    _ = (size, mtime_ns)
    return compute_file_sha256(path_str)


def _get_cached_sha256(file_path: Path, stat: os.stat_result | None = None) -> str:
    """Возвращает SHA-256 файла с кэшированием по (путь, размер, mtime_ns).

    Args:
        file_path: Путь к файлу.
        stat: Предварительно полученный результат stat() или None.

    Returns:
        Шестнадцатеричная строка SHA-256 дайджеста.
    """
    st = stat if stat is not None else file_path.stat()
    return _compute_cached_sha256(str(file_path.resolve()), st.st_size, st.st_mtime_ns)


def list_input_sources(
    input_dir: str | Path,
    db_path: str | Path,
) -> dict[str, Any]:
    """Возвращает список файлов-источников в папке input/ и историю импортов из БД.

    Args:
        input_dir: Путь к папке с исходными файлами (input/).
        db_path: Путь к файлу базы данных SQLite.

    Returns:
        Словарь со списком файлов в input/ ('files') и историей импортов ('history').
    """
    in_path = Path(input_dir)
    in_path.mkdir(parents=True, exist_ok=True)
    ensure_db_initialized(db_path)

    history_rows: list[dict[str, Any]] = []
    history_by_sha: dict[str, dict[str, Any]] = {}
    history_by_name_size: dict[tuple[str, int], dict[str, Any]] = {}

    with open_search_db(db_path) as conn:
        for r in conn.execute("""
            SELECT
                sha256, file_name, file_size, source_date,
                plots_seen, persons_seen, plots_inserted, plots_updated, persons_inserted, imported_at
            FROM import_history
            ORDER BY imported_at DESC
            """):
            item = {
                "sha256": r["sha256"],
                "file_name": r["file_name"],
                "file_size": int(r["file_size"]),
                "source_date": r["source_date"],
                "plots_seen": int(r["plots_seen"]),
                "persons_seen": int(r["persons_seen"]),
                "plots_inserted": int(r["plots_inserted"]),
                "plots_updated": int(r["plots_updated"]),
                "persons_inserted": int(r["persons_inserted"]),
                "imported_at": r["imported_at"],
            }
            history_rows.append(item)
            history_by_sha[r["sha256"]] = item
            history_by_name_size[(r["file_name"], int(r["file_size"]))] = item

    files_info: list[dict[str, Any]] = []
    for p in sorted(in_path.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() not in SUPPORTED_SOURCE_EXTS:
            continue
        st = p.stat()
        detected_date = detect_source_date(p)
        hist = history_by_name_size.get((p.name, st.st_size))
        if hist is None and any(h["file_size"] == st.st_size for h in history_rows):
            hist = history_by_sha.get(_get_cached_sha256(p, st))
        files_info.append(
            {
                "file_name": p.name,
                "file_size": st.st_size,
                "source_date": (
                    hist["source_date"]
                    if hist and hist["source_date"]
                    else detected_date
                ),
                "detected_date": detected_date,
                "imported": hist is not None,
                "imported_at": hist["imported_at"] if hist else None,
                "plots_seen": hist["plots_seen"] if hist else None,
                "persons_seen": hist["persons_seen"] if hist else None,
            }
        )

    return {
        "input_dir": str(in_path.resolve()),
        "files": files_info,
        "history": history_rows,
    }


def save_uploaded_stream_to_input(
    stream: BinaryIO,
    content_length: int,
    raw_filename: str,
    input_dir: str | Path,
    tmp_dir: str | Path,
    progress_cb: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Path, str, bool]:
    """Потоково сохраняет загруженный из браузера файл во временный файл и переносит в input/ без дубликатов.

    Если в папке input/ уже есть файл с таким же размером и SHA-256, временный файл
    удаляется, а функция возвращает путь к уже существующему файлу в input/.

    Args:
        stream: Входной байтовый поток HTTP-запроса.
        content_length: Ожидаемое число байт для чтения из потока.
        raw_filename: Имя файла, переданное браузером.
        input_dir: Папка назначения (input/).
        tmp_dir: Папка для временного файла (tmp/).
        progress_cb: Необязательный колбэк прогресса приёма файла.

    Returns:
        Кортеж (target_path_in_input, sha256_hex, was_duplicate).
    """
    in_path = Path(input_dir)
    t_path = Path(tmp_dir)
    in_path.mkdir(parents=True, exist_ok=True)
    t_path.mkdir(parents=True, exist_ok=True)

    safe_name = Path(raw_filename).name.strip() or "uploaded_dataset.xlsx"
    temp_file = t_path / f".upload_{os.getpid()}_{int(time.time() * 1000)}_{safe_name}"

    hasher = hashlib.sha256()
    bytes_read = 0
    chunk_size = 65536

    try:
        with temp_file.open("wb") as out_f:
            while bytes_read < content_length:
                to_read = min(chunk_size, content_length - bytes_read)
                chunk = stream.read(to_read)
                if not chunk:
                    break
                out_f.write(chunk)
                hasher.update(chunk)
                bytes_read += len(chunk)
                if (
                    progress_cb is not None
                    and bytes_read % (2 * 1024 * 1024) < chunk_size
                ):
                    progress_cb(
                        {
                            "phase": "uploading",
                            "file_name": safe_name,
                            "bytes_received": bytes_read,
                            "bytes_total": content_length,
                        }
                    )

        new_sha256 = hasher.hexdigest()
        new_size = temp_file.stat().st_size

        validate_source_file(temp_file, display_name=safe_name)
        first_plot = next(
            iter_plots_from_file(temp_file, display_name=safe_name),
            None,
        )
        if first_plot is None:
            raise DataValidationError(
                f"В файле «{safe_name}» не найдено ни одной записи о захоронениях "
                f"(проверьте, что файл является выгрузкой набора данных data.mos.ru/opendata/64023)."
            )

        # Проверяем файлы такого же размера в input/ по SHA-256, чтобы не плодить дубликаты
        for existing in in_path.iterdir():
            if not existing.is_file() or existing.name.startswith("."):
                continue
            existing_st = existing.stat()
            if existing_st.st_size == new_size:
                existing_sha = _get_cached_sha256(existing, existing_st)
                if existing_sha == new_sha256:
                    temp_file.unlink(missing_ok=True)
                    return existing, new_sha256, True

        # Если файла с таким содержимым в input/ ещё нет — переносим в input/
        dest = in_path / safe_name
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            idx = 1
            while dest.exists():
                dest = in_path / f"{stem}_{idx}{suffix}"
                idx += 1

        os.replace(temp_file, dest)
        return dest, new_sha256, False
    finally:
        temp_file.unlink(missing_ok=True)
