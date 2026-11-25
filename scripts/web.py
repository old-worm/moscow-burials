"""CLI-обёртка запуска локального веб-интерфейса поиска захоронений."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request
import webbrowser

from src.db import (
    DB_FILE_NAME,
    DEFAULT_DB_PATH,
    ensure_db_initialized,
    migrate_legacy_db_file,
)
from src.diag import (
    log_startup_environment,
    save_and_reveal_diagnostic_zip,
    setup_app_logging,
    show_fatal_error_dialog,
)
from src.web_app import create_http_server

logger = logging.getLogger(__name__)

DEFAULT_PORT = 64023
APP_DATA_FOLDER_NAME = "moscow-burials"


def _is_dir_writable(directory: Path) -> bool:
    """Проверяет возможность создания и записи файлов в указанную директорию.

    Args:
        directory: Путь к проверяемой папке.

    Returns:
        True, если папка существует (или может быть создана) и доступна для записи.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write_probe_{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_app_data_dirs(
    explicit_db: str | None = None,
) -> tuple[Path, Path, Path]:
    """Определяет рабочие пути (db_path, input_dir, tmp_dir) для dev и frozen режимов.

    В режиме разработки из исходников использует относительные папки текущей директории.
    В собранном приложении (PyInstaller):
      - На macOS: ~/Library/Application Support/moscow-burials/
      - На Windows: portable-папка рядом с .exe (с fallback в %LOCALAPPDATA%\\moscow-burials,
        если папка с .exe защищена от записи, например в Program Files).
      - На Linux/прочих ОС: portable-папка рядом с бинарным файлом (с fallback
        в $XDG_DATA_HOME/moscow-burials или ~/.local/share/moscow-burials, если папка
        рядом с бинарником недоступна для записи).

    Args:
        explicit_db: Явно переданный через CLI путь к БД (или None для пути по умолчанию).

    Returns:
        Кортеж (db_path, input_dir, tmp_dir) с абсолютными путями Path.
    """
    if not getattr(sys, "frozen", False):
        db_path = Path(explicit_db or DEFAULT_DB_PATH).resolve()
        input_dir = Path("input").resolve()
        tmp_dir = Path("tmp").resolve()
        return db_path, input_dir, tmp_dir

    if sys.platform == "darwin":
        base_dir = (
            Path.home() / "Library" / "Application Support" / APP_DATA_FOLDER_NAME
        )
    elif sys.platform == "win32":
        exe_dir = Path(sys.executable).resolve().parent
        if _is_dir_writable(exe_dir):
            base_dir = exe_dir
        else:
            local_app_data = Path(
                os.environ.get("LOCALAPPDATA", str(Path.home()))
            ).resolve()
            base_dir = local_app_data / APP_DATA_FOLDER_NAME
    else:
        exe_dir = Path(sys.executable).resolve().parent
        if _is_dir_writable(exe_dir):
            base_dir = exe_dir
        else:
            xdg_raw = os.environ.get("XDG_DATA_HOME", "").strip()
            xdg_data_home = (
                Path(xdg_raw).expanduser().resolve()
                if xdg_raw
                else (Path.home() / ".local" / "share").resolve()
            )
            base_dir = xdg_data_home / APP_DATA_FOLDER_NAME

    db_path = (
        Path(explicit_db).resolve()
        if explicit_db
        else (base_dir / "output" / DB_FILE_NAME).resolve()
    )
    input_dir = (base_dir / "input").resolve()
    tmp_dir = (base_dir / "tmp").resolve()
    return db_path, input_dir, tmp_dir


def _is_our_server_running(host: str, port: int) -> bool:
    """Проверяет, запущен ли уже наш сервер поиска захоронений на указанном хосте и порту.

    Args:
        host: Хост сервера (например, '127.0.0.1').
        port: Порт сервера (например, 64023).

    Returns:
        True, если на порту отвечает эндпоинт /api/meta нашего приложения.
    """
    if port <= 0:
        return False
    check_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{check_host}:{port}/api/meta"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
            return (
                isinstance(data, dict)
                and "total_plots" in data
                and "cemeteries" in data
            )
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False


def _build_parser() -> argparse.ArgumentParser:
    """Создаёт парсер аргументов командной строки для локального веб-сервера.

    Returns:
        Настроенный экземпляр argparse.ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m scripts.web",
        description="Локальный веб-интерфейс поисковой системы захоронений Москвы (64023)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"Путь к файлу БД SQLite (по умолчанию: {DEFAULT_DB_PATH} или папка данных ОС)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Адрес привязки HTTP-сервера (по умолчанию: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=(
            f"Порт HTTP-сервера (по умолчанию: {DEFAULT_PORT}; "
            "если занят — выбирается свободный порт; 0 — автовыбор)"
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Не открывать браузер автоматически при старте",
    )
    parser.add_argument(
        "--diag",
        action="store_true",
        help="Собрать диагностический ZIP-архив с логами и открыть папку с ним",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Запускает локальный HTTP-сервер и открывает интерфейс в браузере.

    Args:
        argv: Список аргументов командной строки (или None для sys.argv).

    Returns:
        Код завершения процесса (0 — успешно, 1 — фатальная ошибка).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    db_path, input_dir, tmp_dir = resolve_app_data_dirs(args.db)
    log_path = (input_dir.parent / "logs" / "app.log").resolve()
    setup_app_logging(log_path)
    if not args.db:
        db_path = migrate_legacy_db_file(db_path)
    log_startup_environment(db_path, input_dir, tmp_dir, log_path)

    if args.diag:
        zip_path = save_and_reveal_diagnostic_zip(
            db_path=db_path,
            input_dir=input_dir,
            tmp_dir=tmp_dir,
            log_path=log_path,
            reveal=not args.no_browser,
        )
        logger.info("Диагностический архив сохранён: %s", zip_path)
        return 0

    try:
        ensure_db_initialized(db_path)

        try:
            server = create_http_server(
                db_path=db_path,
                host=args.host,
                port=args.port,
                input_dir=input_dir,
                tmp_dir=tmp_dir,
                log_path=log_path,
            )
        except OSError:
            if args.port != 0:
                if _is_our_server_running(args.host, args.port):
                    existing_url = f"http://{args.host}:{args.port}/"
                    logger.info("Сервер уже запущен: %s", existing_url)
                    if not args.no_browser:
                        webbrowser.open(existing_url)
                    return 0
                logger.warning(
                    "Порт %d занят другой программой, выбирается свободный порт ОС...",
                    args.port,
                )
                server = create_http_server(
                    db_path=db_path,
                    host=args.host,
                    port=0,
                    input_dir=input_dir,
                    tmp_dir=tmp_dir,
                    log_path=log_path,
                )
            else:
                raise
        actual_host, actual_port = server.server_address[:2]
        url = f"http://{actual_host}:{actual_port}/"

        logger.info("Локальный веб-сервер запущен: %s", url)
        logger.info("База данных: %s", db_path)
        logger.info("Папка импорта (input/): %s", input_dir)
        logger.info("Нажмите Ctrl+C для остановки сервера.")

        if not args.no_browser:
            threading.Timer(0.25, lambda: webbrowser.open(url)).start()

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Остановка сервера по запросу пользователя (Ctrl+C)...")
        finally:
            server.server_close()
            logger.info("Сервер остановлен. Обновление диагностического архива...")
            try:
                save_and_reveal_diagnostic_zip(
                    db_path=db_path,
                    input_dir=input_dir,
                    tmp_dir=tmp_dir,
                    log_path=log_path,
                    reveal=False,
                )
            except OSError:
                pass
        return 0
    except Exception as exc:
        logger.exception("Фатальная ошибка запуска приложения")
        try:
            zip_path = save_and_reveal_diagnostic_zip(
                db_path=db_path,
                input_dir=input_dir,
                tmp_dir=tmp_dir,
                log_path=log_path,
                reveal=True,
            )
            show_fatal_error_dialog(str(exc), zip_path)
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
