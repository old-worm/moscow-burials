"""Диагностическое логирование с ротацией, сборка ZIP-отчёта и аварийный перехватчик старта."""

from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
from typing import Any
import zipfile

from src.db import open_search_db

logger = logging.getLogger(__name__)

DIAGNOSTIC_ZIP_NAME = "moscow-burials-diagnostic.zip"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 1


def setup_app_logging(log_path: str | Path) -> Path:
    """Настраивает корневой логгер и добавляет RotatingFileHandler (1 МБ, 1 ротированный файл).

    При повторном вызове для того же пути не дублирует файловый обработчик.
    На macOS в собранном режиме (.app) дополнительно создаёт симлинк
    ~/Library/Logs/moscow-burials.log -> log_path для просмотра в Console.app.

    Args:
        log_path: Путь к основному лог-файлу (например, logs/app.log).

    Returns:
        Абсолютный путь Path к лог-файлу.
    """
    resolved_log = Path(log_path).resolve()
    resolved_log.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    already_attached = False
    for handler in root_logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            try:
                if Path(handler.baseFilename).resolve() == resolved_log:
                    already_attached = True
                    break
            except OSError:
                continue

    if not already_attached:
        try:
            file_handler = RotatingFileHandler(
                resolved_log,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
            )
            root_logger.addHandler(file_handler)
        except OSError as exc:
            logger.warning(
                "Не удалось открыть лог-файл для записи %s: %s",
                resolved_log,
                exc,
            )

    if getattr(sys, "frozen", False) and sys.platform == "darwin":
        try:
            logs_dir = Path.home() / "Library" / "Logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            symlink_path = logs_dir / "moscow-burials.log"
            if symlink_path.is_symlink() or symlink_path.exists():
                symlink_path.unlink()
            symlink_path.symlink_to(resolved_log)
        except OSError:
            pass

    return resolved_log


def log_startup_environment(
    db_path: Path,
    input_dir: Path,
    tmp_dir: Path,
    log_path: Path,
) -> None:
    """Записывает в лог при старте компактный диагностический паспорт среды и путей.

    Args:
        db_path: Путь к файлу базы данных SQLite.
        input_dir: Путь к папке входных файлов (input/).
        tmp_dir: Путь к папке временных файлов (tmp/).
        log_path: Путь к файлу журнала (app.log).
    """
    mode = "frozen" if getattr(sys, "frozen", False) else "dev"
    py_ver = sys.version.split()[0]
    os_info = platform.platform()
    arch = platform.machine()
    sqlite_ver = sqlite3.sqlite_version

    db_size_mb = (
        round(db_path.stat().st_size / (1024 * 1024), 2) if db_path.is_file() else 0.0
    )
    free_gb: float | None = None
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(db_path.parent)
        free_gb = round(usage.free / (1024**3), 2)
    except OSError:
        pass

    logger.info(
        "Старт Захоронения Москвы | режим=%s | ОС=%s (%s) | Python=%s | SQLite=%s",
        mode,
        os_info,
        arch,
        py_ver,
        sqlite_ver,
    )
    logger.info(
        "Пути данных | db=%s (%.2f МБ) | input=%s | tmp=%s | log=%s | свободно_на_диске=%s ГБ",
        db_path,
        db_size_mb,
        input_dir,
        tmp_dir,
        log_path,
        free_gb if free_gb is not None else "н/д",
    )


def collect_diagnostic_report(
    db_path: Path,
    input_dir: Path,
    tmp_dir: Path,
    log_path: Path,
) -> dict[str, Any]:
    """Собирает словарь диагностических метаданных об окружении, файлах и состоянии БД.

    Args:
        db_path: Путь к файлу базы данных SQLite.
        input_dir: Путь к папке входных файлов (input/).
        tmp_dir: Путь к папке временных файлов (tmp/).
        log_path: Путь к файлу журнала (app.log).

    Returns:
        Сериализуемый в JSON словарь диагностической сводки.
    """
    resolved_db = Path(db_path).resolve()
    resolved_input = Path(input_dir).resolve()
    resolved_tmp = Path(tmp_dir).resolve()
    resolved_log = Path(log_path).resolve()

    disk_info: dict[str, Any] = {}
    try:
        resolved_db.parent.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(resolved_db.parent)
        disk_info = {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_gb": round(usage.free / (1024**3), 2),
        }
    except OSError as exc:
        disk_info = {"error": str(exc)}

    input_files: list[dict[str, Any]] = []
    if resolved_input.is_dir():
        try:
            for item in sorted(resolved_input.iterdir(), key=lambda p: p.name.lower()):
                if item.is_file() and not item.name.startswith("."):
                    try:
                        st = item.stat()
                        input_files.append(
                            {
                                "name": item.name,
                                "size_bytes": st.st_size,
                                "mtime_utc": datetime.fromtimestamp(
                                    st.st_mtime, tz=timezone.utc
                                ).isoformat(),
                            }
                        )
                    except OSError:
                        continue
        except OSError:
            pass

    db_meta: dict[str, Any] = {
        "exists": resolved_db.is_file(),
        "size_bytes": resolved_db.stat().st_size if resolved_db.is_file() else 0,
        "total_plots": 0,
        "total_persons": 0,
        "cemeteries_count": 0,
        "import_history": [],
    }
    if resolved_db.is_file():
        try:
            with open_search_db(resolved_db) as conn:
                db_meta["total_plots"] = int(
                    conn.execute("SELECT COUNT(*) FROM plots;").fetchone()[0]
                )
                db_meta["total_persons"] = int(
                    conn.execute("SELECT COUNT(*) FROM persons;").fetchone()[0]
                )
                db_meta["cemeteries_count"] = int(
                    conn.execute("SELECT COUNT(*) FROM cemeteries;").fetchone()[0]
                )
                rows = conn.execute("""
                    SELECT file_name, file_size, source_date, plots_seen, persons_seen,
                           plots_inserted, plots_updated, persons_inserted, imported_at
                    FROM import_history
                    ORDER BY id DESC
                    LIMIT 50;
                    """).fetchall()
                db_meta["import_history"] = [dict(r) for r in rows]
        except (sqlite3.Error, OSError) as exc:
            db_meta["error"] = str(exc)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "app": {
            "mode": "frozen" if getattr(sys, "frozen", False) else "dev",
            "executable": sys.executable,
            "argv": sys.argv,
        },
        "system": {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python_version": sys.version,
            "sqlite_version": sqlite3.sqlite_version,
        },
        "paths": {
            "db_path": str(resolved_db),
            "input_dir": str(resolved_input),
            "tmp_dir": str(resolved_tmp),
            "log_path": str(resolved_log),
        },
        "disk": disk_info,
        "database": db_meta,
        "input_files": input_files,
    }


def build_diagnostic_zip_bytes(
    db_path: Path,
    input_dir: Path,
    tmp_dir: Path,
    log_path: Path,
) -> bytes:
    """Формирует в памяти сжатый ZIP-архив с diagnostic_info.json, app.log и app.log.1.

    Args:
        db_path: Путь к файлу базы данных SQLite.
        input_dir: Путь к папке входных файлов (input/).
        tmp_dir: Путь к папке временных файлов (tmp/).
        log_path: Путь к основному лог-файлу (app.log).

    Returns:
        Байты сформированного ZIP-архива.
    """
    resolved_log = Path(log_path).resolve()
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except OSError:
            pass

    report = collect_diagnostic_report(
        db_path=Path(db_path).resolve(),
        input_dir=Path(input_dir).resolve(),
        tmp_dir=Path(tmp_dir).resolve(),
        log_path=resolved_log,
    )
    report_bytes = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("diagnostic_info.json", report_bytes)
        for candidate_name in (resolved_log.name, f"{resolved_log.name}.1"):
            candidate_path = resolved_log.parent / candidate_name
            if candidate_path.is_file():
                try:
                    zf.write(candidate_path, arcname=candidate_name)
                except OSError:
                    pass
    return buf.getvalue()


def reveal_path_in_file_manager(target_path: Path) -> None:
    """Открывает папку в системном файловом менеджере (Finder / Проводник / xdg-open), выделяя файл.

    Корректно работает с путями, содержащими кириллицу и пробелы, на Windows и macOS.

    Args:
        target_path: Путь к файлу или папке, которую необходимо показать пользователю.
    """
    resolved = Path(os.path.abspath(os.fspath(target_path)))
    try:
        if sys.platform == "darwin":
            if resolved.exists():
                subprocess.Popen(["open", "-R", str(resolved)])
            else:
                subprocess.Popen(["open", str(resolved.parent)])
        elif os.name == "nt" or sys.platform == "win32":
            win_path = os.path.normpath(str(resolved))
            if resolved.is_file():
                subprocess.Popen(f'explorer.exe /select,"{win_path}"')
            else:
                folder = (
                    win_path
                    if resolved.is_dir()
                    else os.path.normpath(str(resolved.parent))
                )
                if hasattr(os, "startfile"):
                    os.startfile(folder)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["explorer.exe", folder])
        else:
            folder = resolved if resolved.is_dir() else resolved.parent
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError as exc:
        logger.warning(
            "Не удалось открыть файловый менеджер для %s: %s",
            resolved,
            exc,
        )


def save_and_reveal_diagnostic_zip(
    db_path: Path,
    input_dir: Path,
    tmp_dir: Path,
    log_path: Path,
    reveal: bool = True,
) -> Path:
    """Сохраняет moscow-burials-diagnostic.zip в папку логов (logs/) и открывает её в Finder/Проводнике.

    Никогда не сохраняет файлы на Рабочий стол пользователя.

    Args:
        db_path: Путь к файлу базы данных SQLite.
        input_dir: Путь к папке входных файлов (input/).
        tmp_dir: Путь к папке временных файлов (tmp/).
        log_path: Путь к основному лог-файлу (logs/app.log).
        reveal: Открыть ли папку с выделенным архивом в системном файловом менеджере.

    Returns:
        Путь Path к сохранённому файлу moscow-burials-diagnostic.zip.
    """
    resolved_log = Path(log_path).resolve()
    resolved_log.parent.mkdir(parents=True, exist_ok=True)
    zip_path = resolved_log.parent / DIAGNOSTIC_ZIP_NAME
    zip_bytes = build_diagnostic_zip_bytes(
        db_path=db_path,
        input_dir=input_dir,
        tmp_dir=tmp_dir,
        log_path=resolved_log,
    )
    zip_path.write_bytes(zip_bytes)
    if reveal:
        reveal_path_in_file_manager(zip_path)
    return zip_path


def show_fatal_error_dialog(error_msg: str, zip_path: Path) -> None:
    """Показывает нативное окно ОС при фатальной ошибке запуска приложения.

    Args:
        error_msg: Текст возникшего исключения.
        zip_path: Путь к сформированному диагностическому архиву moscow-burials-diagnostic.zip.
    """
    text = (
        f"Не удалось запустить приложение «Захоронения Москвы».\n\n"
        f"Причина: {error_msg}\n\n"
        f"Диагностический архив с логом сохранён в папку программы:\n{zip_path}"
    )
    try:
        if sys.platform == "darwin":
            safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
            script = (
                'display alert "Ошибка запуска Захоронения Москвы" '
                f'message "{safe_text}" as critical'
            )
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        elif os.name == "nt" or sys.platform == "win32":
            windll = getattr(ctypes, "windll", None)
            if windll is not None:
                windll.user32.MessageBoxW(
                    0,
                    text,
                    "Ошибка запуска — Захоронения Москвы",
                    0x10,
                )
        else:
            subprocess.run(
                [
                    "zenity",
                    "--error",
                    "--title=Ошибка запуска — Захоронения Москвы",
                    f"--text={text}",
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
    except (OSError, subprocess.SubprocessError):
        pass
