"""Управление схемой базы данных SQLite, индексами FTS5 и настройками PRAGMA."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import sqlite3

logger = logging.getLogger(__name__)

DB_FILE_NAME = "burials.db"
# Имя файла базы в версии 0.1.0; при запуске переименовывается в DB_FILE_NAME.
LEGACY_DB_FILE_NAME = "graves.db"
DEFAULT_DB_PATH = f"output/{DB_FILE_NAME}"
# Основной файл SQLite и его служебные файлы (WAL, общая память, журнал отката).
SQLITE_FILE_SUFFIXES = ("", "-wal", "-shm", "-journal")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cemeteries (
    id INTEGER PRIMARY KEY,
    code TEXT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS plots (
    global_id INTEGER PRIMARY KEY,
    cemetery_id INTEGER NOT NULL REFERENCES cemeteries(id),
    place_number TEXT,
    plot_section TEXT,
    grave_type INTEGER NOT NULL,
    has_tombstone INTEGER NOT NULL,
    person_count INTEGER NOT NULL DEFAULT 0,
    lon REAL,
    lat REAL,
    area REAL,
    source_date TEXT
);

CREATE TABLE IF NOT EXISTS import_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    source_date TEXT,
    plots_seen INTEGER NOT NULL DEFAULT 0,
    persons_seen INTEGER NOT NULL DEFAULT 0,
    plots_inserted INTEGER NOT NULL DEFAULT 0,
    plots_updated INTEGER NOT NULL DEFAULT 0,
    persons_inserted INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY,
    global_id INTEGER NOT NULL REFERENCES plots(global_id),
    raw_line TEXT NOT NULL,
    surname TEXT,
    firstname TEXT,
    patronymic TEXT,
    fio_norm TEXT NOT NULL,
    birth_year INTEGER,
    birth_date_raw TEXT,
    death_year INTEGER,
    death_date_raw TEXT,
    single_date_year INTEGER,
    single_date_raw TEXT
);

CREATE INDEX IF NOT EXISTS idx_plots_cemetery_section
    ON plots(cemetery_id, plot_section);

CREATE INDEX IF NOT EXISTS idx_persons_global_id
    ON persons(global_id, birth_year, death_year);

CREATE VIRTUAL TABLE IF NOT EXISTS persons_fts USING fts5(
    surname, firstname, patronymic, fio_norm,
    content='persons', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE VIRTUAL TABLE IF NOT EXISTS persons_vocab USING fts5vocab(persons_fts, 'row');

CREATE VIRTUAL TABLE IF NOT EXISTS persons_trigram USING fts5(
    fio_norm,
    content='persons', content_rowid='id',
    tokenize='trigram'
);
"""


def _ensure_migrations(conn: sqlite3.Connection) -> None:
    """Выполняет безопасную миграцию существующей схемы БД при обновлении версии.

    Args:
        conn: Открытое соединение с базой данных SQLite.
    """
    plot_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(plots)").fetchall()}
    if "source_date" not in plot_cols:
        conn.execute("ALTER TABLE plots ADD COLUMN source_date TEXT;")

    hist_cols = {
        str(r[1]) for r in conn.execute("PRAGMA table_info(import_history)").fetchall()
    }
    if hist_cols and "persons_seen" not in hist_cols:
        conn.execute(
            "ALTER TABLE import_history ADD COLUMN persons_seen INTEGER NOT NULL DEFAULT 0;"
        )


def ensure_db_initialized(db_path: str | Path) -> None:
    """Создаёт файл базы данных и таблицы схемы, если база ещё не инициализирована.

    Args:
        db_path: Путь к файлу базы данных SQLite.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    try:
        conn.executescript(SCHEMA_SQL)
        _ensure_migrations(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def open_import_db(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Открывает соединение SQLite в режиме потокового импорта с малым потреблением RAM.

    Args:
        db_path: Путь к файлу базы данных SQLite.

    Yields:
        Настроенное соединение sqlite3.Connection, автоматически закрываемое при выходе.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA temp_store = DEFAULT;")
        conn.execute("PRAGMA cache_size = -16000;")
        conn.executescript(SCHEMA_SQL)
        _ensure_migrations(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def open_search_db(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Открывает соединение SQLite в режиме чтения с поддержкой конкурентного доступа WAL.

    Корректно обрабатывает длинные пути с кириллицей и пробелами на Windows и POSIX.

    Args:
        db_path: Путь к файлу базы данных SQLite.

    Yields:
        Настроенное соединение sqlite3.Connection в режиме read-only.
    """
    path = Path(os.path.abspath(os.fspath(db_path)))
    if not path.is_file():
        ensure_db_initialized(path)
    uri = f"{path.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000;")
        conn.execute("PRAGMA temp_store = DEFAULT;")
        conn.execute("PRAGMA mmap_size = 268435456;")
        yield conn
    finally:
        conn.close()


FTS_REBUILD_STEPS: tuple[tuple[str, str], ...] = (
    ("persons_fts", "индекс по словам"),
    ("persons_trigram", "индекс по фрагментам слов"),
)


def rebuild_fts_indexes(
    conn: sqlite3.Connection,
    on_step: Callable[[int, int, str], None] | None = None,
) -> None:
    """Полностью перестраивает внешние полнотекстовые индексы persons_fts и persons_trigram.

    Не фиксирует транзакцию: commit выполняет вызывающий код (импорт делает его один раз
    в самом конце, чтобы остановка импорта откатывала и данные, и индексы).

    Args:
        conn: Открытое соединение с базой данных SQLite.
        on_step: Необязательный колбэк перед каждым шагом: (номер шага с 1, всего шагов, описание).
    """
    total = len(FTS_REBUILD_STEPS)
    for idx, (table, label) in enumerate(FTS_REBUILD_STEPS, start=1):
        if on_step is not None:
            on_step(idx, total, label)
        conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild');")


def migrate_legacy_db_file(db_path: str | Path) -> Path:
    """Переименовывает базу со старым именем graves.db в burials.db в той же папке.

    Срабатывает, только если db_path указывает на burials.db, его ещё нет, а рядом лежит
    graves.db. Служебные файлы SQLite (-wal, -shm, -journal) переносятся вместе с базой:
    без -wal можно потерять последние изменения. Если переименовать не удалось (например,
    файл открыт другой копией программы), уже перенесённые файлы возвращаются обратно
    и дальше используется старый файл — данные не теряются.

    Args:
        db_path: Путь к базе с новым именем (burials.db).

    Returns:
        Путь к базе, с которой следует работать (новый или, при неудаче, старый).
    """
    path = Path(db_path)
    legacy = path.with_name(LEGACY_DB_FILE_NAME)
    if path.name != DB_FILE_NAME or path.exists() or not legacy.is_file():
        return path

    # Основной файл переносится последним: пока он на старом месте, база считается старой.
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in reversed(SQLITE_FILE_SUFFIXES):
            src = Path(f"{legacy}{suffix}")
            dst = Path(f"{path}{suffix}")
            if src.exists():
                src.rename(dst)
                moved.append((src, dst))
    except OSError as exc:
        for src, dst in reversed(moved):
            try:
                dst.rename(src)
            except OSError:
                logger.exception("Не удалось вернуть %s на место %s", dst, src)
        logger.warning(
            "Не удалось переименовать базу %s в %s (%s); используется старое имя.",
            legacy,
            path.name,
            exc,
        )
        return legacy

    logger.info("База данных переименована: %s -> %s", legacy, path)
    return path


def clear_db(db_path: str | Path) -> None:
    """Полностью очищает базу данных SQLite и пересоздаёт пустую схему.

    Исходные файлы в папке input/ при этом не затрагиваются.

    Args:
        db_path: Путь к файлу базы данных SQLite.
    """
    path = Path(db_path).resolve()
    try:
        for suffix in SQLITE_FILE_SUFFIXES:
            candidate = Path(f"{path}{suffix}")
            if candidate.exists():
                candidate.unlink()
        ensure_db_initialized(path)
    except OSError:
        conn = sqlite3.connect(str(path), timeout=30.0)
        try:
            conn.executescript("""
                DELETE FROM persons;
                DELETE FROM plots;
                DELETE FROM cemeteries;
                DELETE FROM import_history;
                INSERT INTO persons_fts(persons_fts) VALUES('rebuild');
                INSERT INTO persons_trigram(persons_trigram) VALUES('rebuild');
                """)
            conn.commit()
            conn.execute("VACUUM;")
        finally:
            conn.close()
