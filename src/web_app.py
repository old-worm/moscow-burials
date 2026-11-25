"""HTTP-обработчик, JSON API и раздача статики локального веб-интерфейса поиска захоронений."""

from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from src import __version__
from src.db import clear_db, ensure_db_initialized
from src.diag import (
    DIAGNOSTIC_ZIP_NAME,
    build_diagnostic_zip_bytes,
    save_and_reveal_diagnostic_zip,
)
from src.importer import (
    DataValidationError,
    ImportCancelledError,
    ImportCancelToken,
    import_file,
    list_input_sources,
    save_uploaded_stream_to_input,
)
from src.models import PersonFilter, SearchQuery
from src.parser import format_display_inscription
from src.search import get_db_meta, search_plots

logger = logging.getLogger(__name__)

STATIC_ROUTES: dict[str, tuple[str, str]] = {
    "/": ("static/index.html", "text/html; charset=utf-8"),
    "/index.html": ("static/index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("static/styles.css", "text/css; charset=utf-8"),
    "/static/app.js": ("static/app.js", "application/javascript; charset=utf-8"),
    "/coat_of_arms.svg": ("static/coat_of_arms.svg", "image/svg+xml; charset=utf-8"),
    "/favicon.ico": ("static/coat_of_arms.svg", "image/svg+xml; charset=utf-8"),
}


def _resolve_static_path(relative_path: str) -> Path:
    """Определяет путь к файлу статического ресурса внутри пакета src/ (с учётом PyInstaller).

    Args:
        relative_path: Относительный путь внутри src/ (например, 'static/index.html').

    Returns:
        Объект Path к статическому файлу.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "src" / relative_path
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parent / relative_path


def _load_static_bytes(relative_path: str) -> bytes | None:
    """Читает байты статического ресурса из src/static/ или src/ (с поддержкой sys._MEIPASS).

    Args:
        relative_path: Относительный путь внутри src/ (например, 'static/index.html').

    Returns:
        Содержимое файла в байтах или None, если файл не найден.
    """
    path = _resolve_static_path(relative_path)
    if not path.is_file():
        return None
    data = path.read_bytes()
    if relative_path == "static/index.html":
        data = data.replace(b"{{APP_VERSION}}", __version__.encode("ascii"))
    return data


def _first_param(qs: dict[str, list[str]], *names: str) -> str:
    """Возвращает первое непустое строковое значение из query-параметров.

    Args:
        qs: Словарь query-параметров, возвращённый urllib.parse.parse_qs.
        *names: Имена параметров в порядке приоритета.

    Returns:
        Очищенное строковое значение первого найденного параметра или пустая строка.
    """
    for name in names:
        vals = qs.get(name)
        if vals and vals[0].strip():
            return vals[0].strip()
    return ""


def _int_param(qs: dict[str, list[str]], *names: str) -> int | None:
    """Преобразует строковый query-параметр в целое число или None.

    Args:
        qs: Словарь query-параметров, возвращённый urllib.parse.parse_qs.
        *names: Имена параметров в порядке приоритета.

    Returns:
        Целое число или None, если параметр не задан.

    Raises:
        ValueError: Если значение параметра не является корректным целым числом.
    """
    raw = _first_param(qs, *names)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Некорректное числовое значение параметра: {raw!r}") from exc


def parse_search_query_from_qs(qs: dict[str, list[str]]) -> SearchQuery:
    """Разбирает словарь query-параметров URL в типизированный объект SearchQuery.

    Args:
        qs: Словарь, возвращённый urllib.parse.parse_qs.

    Returns:
        Экземпляр SearchQuery.

    Raises:
        ValueError: Если числовой параметр имеет некорректный формат.
    """
    p1 = PersonFilter(
        fio=_first_param(qs, "fio1", "fio"),
        search_mode=_first_param(qs, "mode1", "mode") or "prefix_default",
        birth_exact=_first_param(qs, "birth1_exact", "birth_exact") or None,
        birth_from=_int_param(qs, "birth1_from", "birth_from"),
        birth_to=_int_param(qs, "birth1_to", "birth_to"),
        death_exact=_first_param(qs, "death1_exact", "death_exact") or None,
        death_from=_int_param(qs, "death1_from", "death_from"),
        death_to=_int_param(qs, "death1_to", "death_to"),
    )

    p2 = PersonFilter(
        fio=_first_param(qs, "fio2"),
        search_mode=_first_param(qs, "mode2") or "prefix_default",
        birth_exact=_first_param(qs, "birth2_exact") or None,
        birth_from=_int_param(qs, "birth2_from"),
        birth_to=_int_param(qs, "birth2_to"),
        death_exact=_first_param(qs, "death2_exact") or None,
        death_from=_int_param(qs, "death2_from"),
        death_to=_int_param(qs, "death2_to"),
    )

    return SearchQuery(
        person1=p1,
        person2=p2 if p2.is_active() else None,
        cemetery_id=_int_param(qs, "cemetery_id"),
        cemetery_name=_first_param(qs, "cemetery", "cemetery_name") or None,
        place_number=_first_param(qs, "place_number", "place") or None,
        global_id=_int_param(qs, "global_id"),
        limit=_int_param(qs, "limit") or 50,
        offset=_int_param(qs, "offset") or 0,
    )


def build_csv_export(db_path: str | Path, query: SearchQuery) -> bytes:
    """Формирует CSV-выгрузку найденных участков в кодировке UTF-8 с BOM.

    Args:
        db_path: Путь к файлу базы данных SQLite.
        query: Поисковый запрос SearchQuery.

    Returns:
        Байты CSV-файла (utf-8-sig).
    """
    resp = search_plots(db_path, query)
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "global_id",
            "Кладбище",
            "Тип захоронения",
            "Участок",
            "Шифр места",
            "Площадь (м2)",
            "Широта",
            "Долгота",
            "Надписи на памятнике",
            "Яндекс.Карты",
            "Схема epoisk",
        ]
    )
    for plot in resp.results:
        person_entries = [
            format_display_inscription(
                p.raw_line,
                p.birth_date_raw,
                p.death_date_raw,
                p.single_date_raw,
            )
            for p in plot.persons
        ]
        inscriptions_str = " | ".join(person_entries)
        writer.writerow(
            [
                plot.global_id,
                plot.cemetery_name,
                plot.grave_type_label,
                plot.plot_section or "",
                plot.place_number or "",
                plot.area if plot.area is not None else "",
                plot.lat if plot.lat is not None else "",
                plot.lon if plot.lon is not None else "",
                inscriptions_str,
                plot.ya_url or "",
                plot.epoisk_map_url or "",
            ]
        )
    return buf.getvalue().encode("utf-8-sig")


def _open_directory_in_file_manager(directory: Path) -> None:
    """Открывает указанную папку в системном файловом менеджере (Finder / Explorer / xdg-open).

    Args:
        directory: Путь к папке, которую требуется открыть.

    Raises:
        OSError: Если не удалось запустить системный файловый менеджер.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(directory)])
    elif os.name == "nt":
        if hasattr(os, "startfile"):
            os.startfile(str(directory))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["explorer", str(directory)])
    else:
        subprocess.Popen(["xdg-open", str(directory)])


def create_http_server(
    db_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 0,
    input_dir: str | Path = "input",
    tmp_dir: str | Path = "tmp",
    idle_timeout_sec: float = 600.0,
    log_path: str | Path | None = None,
) -> ThreadingHTTPServer:
    """Создаёт настроенный экземпляр локального HTTP-сервера.

    Args:
        db_path: Путь к файлу базы данных SQLite.
        host: Хост привязки (по умолчанию 127.0.0.1).
        port: Порт (0 — автоматический выбор свободного порта ОС).
        input_dir: Папка для хранения файлов-источников (по умолчанию: input).
        tmp_dir: Папка для временных файлов при потоковой загрузке (по умолчанию: tmp).
        idle_timeout_sec: Таймаут автовыключения сервера при отсутствии активных вкладок
            (по умолчанию 600 секунд = 10 минут; <= 0 отключает автовыключение).
        log_path: Путь к файлу журнала (по умолчанию: папка logs/app.log рядом с output/).

    Returns:
        Экземпляр ThreadingHTTPServer.
    """
    resolved_db = Path(db_path).resolve()
    ensure_db_initialized(resolved_db)
    resolved_input = Path(input_dir).resolve()
    resolved_tmp = Path(tmp_dir).resolve()
    if log_path is not None:
        resolved_log = Path(log_path).resolve()
    else:
        base_dir = (
            resolved_db.parent.parent
            if resolved_db.parent.name == "output"
            else resolved_db.parent
        )
        resolved_log = (base_dir / "logs" / "app.log").resolve()
    resolved_input.mkdir(parents=True, exist_ok=True)
    resolved_tmp.mkdir(parents=True, exist_ok=True)

    cached_meta: dict[str, Any] | None = None
    job_lock = threading.Lock()
    client_lock = threading.Lock()
    active_clients: dict[str, float] = {}
    last_client_activity: float = time.monotonic()

    def _prune_stale_clients_locked(now: float) -> None:
        """Удаляет из active_clients вкладки, не присылавшие heartbeat дольше idle_timeout_sec."""
        if idle_timeout_sec <= 0:
            return
        stale_ids = [
            cid
            for cid, seen_at in active_clients.items()
            if (now - seen_at) >= idle_timeout_sec
        ]
        for cid in stale_ids:
            active_clients.pop(cid, None)

    def _make_import_job_state(**overrides: Any) -> dict[str, Any]:
        """Формирует начальный словарь состояния задачи импорта с переопределениями."""
        state: dict[str, Any] = {
            "running": False,
            "phase": "idle",
            "file_name": None,
            "source_date": None,
            "plots_seen": 0,
            "persons_seen": 0,
            "plots_inserted": 0,
            "plots_updated": 0,
            "persons_inserted": 0,
            "elapsed_sec": 0.0,
            "was_duplicate": False,
            "cancel_requested": False,
            "error": None,
        }
        state.update(overrides)
        return state

    import_job: dict[str, Any] = _make_import_job_state()
    # Признак отмены текущего фонового импорта (None — импорт не выполняется).
    current_cancel_token: ImportCancelToken | None = None
    # Этапы, на которых импорт можно остановить с полным откатом изменений в базе.
    cancellable_phases = ("hashing", "reading", "indexing")
    # Монотонные отметки времени для «живого» таймера: пока идёт долгий шаг
    # (например, перестроение FTS-индексов), импорт не присылает обновлений,
    # и без досчёта время в интерфейсе застывало бы.
    job_clock: dict[str, Any] = {
        "phase": None,
        "step": None,
        "phase_since": 0.0,
        "step_since": 0.0,
        "updated_at": 0.0,
    }

    def _sync_job_clock(now: float) -> None:
        """Сбрасывает отсчёт этапа/шага при их смене. Вызывать под job_lock."""
        phase = import_job.get("phase")
        step = import_job.get("index_step")
        if phase != job_clock["phase"]:
            job_clock.update(phase=phase, phase_since=now, step=step, step_since=now)
        elif step != job_clock["step"]:
            job_clock.update(step=step, step_since=now)

    def _update_progress(info: dict[str, Any]) -> None:
        now = time.monotonic()
        with job_lock:
            import_job.update(info)
            _sync_job_clock(now)
            job_clock["updated_at"] = now

    def _import_status_snapshot() -> dict[str, Any]:
        """Снимок состояния импорта с досчитанным до текущего момента временем."""
        now = time.monotonic()
        with job_lock:
            snapshot = dict(import_job)
            if not snapshot.get("running"):
                return snapshot
            _sync_job_clock(now)
            snapshot["phase_elapsed_sec"] = now - job_clock["phase_since"]
            snapshot["step_elapsed_sec"] = now - job_clock["step_since"]
            if snapshot.get("phase") in ("reading", "indexing") and job_clock["updated_at"]:
                snapshot["elapsed_sec"] = float(snapshot.get("elapsed_sec") or 0.0) + (
                    now - job_clock["updated_at"]
                )
            return snapshot

    def _start_background_import(
        target_file: Path,
        src_date: str | None,
        known_sha256: str | None = None,
        was_dup: bool = False,
        remove_on_validation_error: bool = False,
    ) -> dict[str, Any]:
        nonlocal cached_meta, current_cancel_token
        cancel_token = ImportCancelToken()
        with job_lock:
            if import_job["running"]:
                raise RuntimeError("Импорт другого файла уже выполняется")
            current_cancel_token = cancel_token
            import_job.clear()
            import_job.update(
                _make_import_job_state(
                    running=True,
                    phase="hashing",
                    file_name=target_file.name,
                    source_date=src_date,
                    was_duplicate=was_dup,
                )
            )
            snapshot = dict(import_job)

        def _worker() -> None:
            nonlocal cached_meta, current_cancel_token
            logger.info(
                "Старт фонового импорта: файл=%s | дата=%s | дубликат=%s",
                target_file.name,
                src_date or "авто",
                was_dup,
            )
            try:
                stats = import_file(
                    file_path=target_file,
                    db_path=resolved_db,
                    source_date=src_date,
                    file_sha256=known_sha256,
                    progress_cb=_update_progress,
                    cancel_token=cancel_token,
                )
                cached_meta = None
                with job_lock:
                    import_job["running"] = False
                logger.info(
                    "Фоновый импорт завершён: файл=%s | прочитано=%d уч. (%d надп.) | "
                    "добавлено=%d уч. | обновлено=%d уч. | записано=%d надп. | %.2f с",
                    target_file.name,
                    stats.plots_seen,
                    stats.persons_seen,
                    stats.plots_inserted,
                    stats.plots_updated,
                    stats.persons_inserted,
                    stats.elapsed_sec,
                )
            except ImportCancelledError:
                logger.info(
                    "Фоновый импорт остановлен пользователем, изменения откатаны: файл=%s",
                    target_file.name,
                )
                with job_lock:
                    import_job["running"] = False
                    import_job["phase"] = "cancelled"
            except Exception as exc:
                logger.exception("Ошибка фонового импорта файла %s", target_file)
                if remove_on_validation_error and isinstance(exc, DataValidationError):
                    try:
                        target_file.unlink(missing_ok=True)
                    except OSError:
                        pass
                with job_lock:
                    import_job["running"] = False
                    import_job["phase"] = "error"
                    import_job["error"] = str(exc)
            finally:
                with job_lock:
                    if current_cancel_token is cancel_token:
                        current_cancel_token = None
                try:
                    save_and_reveal_diagnostic_zip(
                        db_path=resolved_db,
                        input_dir=resolved_input,
                        tmp_dir=resolved_tmp,
                        log_path=resolved_log,
                        reveal=False,
                    )
                except OSError:
                    pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return snapshot

    class GravesRequestHandler(BaseHTTPRequestHandler):
        """Обработчик HTTP-запросов локального веб-интерфейса."""

        def _send_bytes(
            self,
            body: bytes,
            content_type: str,
            status: int = HTTPStatus.OK,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            """Отправляет HTTP-ответ с защитой от разрыва соединения клиентом.

            Args:
                body: Тело ответа в байтах.
                content_type: Значение заголовка Content-Type.
                status: HTTP-код ответа.
                extra_headers: Дополнительные HTTP-заголовки.
            """
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if extra_headers:
                    for h_name, h_val in extra_headers.items():
                        self.send_header(h_name, h_val)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            """Сериализует объект в JSON и отправляет клиенту.

            Args:
                payload: Сериализуемый в JSON объект.
                status: HTTP-код ответа.
            """
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_bytes(
                body,
                "application/json; charset=utf-8",
                status=status,
                extra_headers={"Cache-Control": "no-store"},
            )

        def _send_csv(self, csv_bytes: bytes, filename: str) -> None:
            """Отправляет CSV-файл как вложение для скачивания.

            Args:
                csv_bytes: Байты CSV-файла.
                filename: Имя файла для заголовка Content-Disposition.
            """
            self._send_bytes(
                csv_bytes,
                "text/csv; charset=utf-8",
                status=HTTPStatus.OK,
                extra_headers={
                    "Content-Disposition": f'attachment; filename="{filename}"'
                },
            )

        def do_GET(self) -> None:
            """Обрабатывает входящие HTTP GET-запросы."""
            nonlocal cached_meta
            parsed = urlparse(self.path)
            route = parsed.path

            if route in STATIC_ROUTES:
                rel_path, content_type = STATIC_ROUTES[route]
                static_bytes = _load_static_bytes(rel_path)
                if static_bytes is not None:
                    extra = (
                        {"Cache-Control": "public, max-age=86400"}
                        if rel_path.endswith("coat_of_arms.svg")
                        else {"Cache-Control": "no-cache"}
                    )
                    self._send_bytes(
                        static_bytes,
                        content_type,
                        status=HTTPStatus.OK,
                        extra_headers=extra,
                    )
                    return

            if route == "/api/meta":
                try:
                    if cached_meta is None:
                        cached_meta = get_db_meta(resolved_db)
                    self._send_json(cached_meta)
                except (sqlite3.Error, OSError) as exc:
                    logger.exception("Ошибка при получении /api/meta")
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            if route == "/api/sources":
                try:
                    info = list_input_sources(resolved_input, resolved_db)
                    info["input_dir"] = str(resolved_input)
                    info["db_path"] = str(resolved_db)
                    info["job"] = _import_status_snapshot()
                    self._send_json(info)
                except (sqlite3.Error, OSError) as exc:
                    logger.exception("Ошибка при получении /api/sources")
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            if route == "/api/import/status":
                self._send_json(_import_status_snapshot())
                return

            if route == "/api/search":
                try:
                    qs = parse_qs(parsed.query, keep_blank_values=False)
                    query = parse_search_query_from_qs(qs)
                    resp = search_plots(resolved_db, query)
                    logger.info(
                        "Поиск: query=%s -> найдено %d уч. за %.1f мс",
                        unquote(parsed.query),
                        resp.total_plots,
                        resp.elapsed_ms,
                    )
                    self._send_json(asdict(resp))
                except ValueError as exc:
                    logger.info(
                        "Некорректный параметр поиска (%s): %s",
                        unquote(parsed.query),
                        exc,
                    )
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except (sqlite3.Error, OSError) as exc:
                    logger.exception("Ошибка при выполнении /api/search")
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            if route == "/api/export.csv":
                try:
                    qs = parse_qs(parsed.query, keep_blank_values=False)
                    query = parse_search_query_from_qs(qs)
                    query.limit = min(max(1, query.limit), 1000)
                    csv_bytes = build_csv_export(resolved_db, query)
                    logger.info(
                        "Экспорт CSV: query=%s (%d байт)",
                        unquote(parsed.query),
                        len(csv_bytes),
                    )
                    export_name = (
                        f"burials-export-{datetime.now():%Y-%m-%d_%H-%M-%S}.csv"
                    )
                    self._send_csv(csv_bytes, export_name)
                except ValueError as exc:
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except (sqlite3.Error, OSError) as exc:
                    logger.exception("Ошибка при экспорте CSV")
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            if route == "/api/log.zip":
                try:
                    zip_bytes = build_diagnostic_zip_bytes(
                        db_path=resolved_db,
                        input_dir=resolved_input,
                        tmp_dir=resolved_tmp,
                        log_path=resolved_log,
                    )
                    self._send_bytes(
                        zip_bytes,
                        "application/zip",
                        status=HTTPStatus.OK,
                        extra_headers={
                            "Content-Disposition": f'attachment; filename="{DIAGNOSTIC_ZIP_NAME}"',
                            "Cache-Control": "no-store",
                        },
                    )
                except OSError as exc:
                    logger.exception("Ошибка при сборке /api/log.zip")
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            self._send_json(
                {"error": f"Маршрут не найден: {route}"},
                status=HTTPStatus.NOT_FOUND,
            )

        def _extract_client_id(self, parsed_query: str) -> str:
            """Извлекает идентификатор вкладки client_id из query-параметра или JSON-тела.

            Args:
                parsed_query: Query-строка URL запроса.

            Returns:
                Строковый идентификатор клиента (или 'anonymous', если не передан).
            """
            qs = parse_qs(parsed_query, keep_blank_values=False)
            cid = _first_param(qs, "client_id")
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = b""
            if length > 0:
                raw_body = self.rfile.read(length)
            if not cid and raw_body:
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                    if isinstance(payload, dict) and payload.get("client_id"):
                        cid = str(payload["client_id"]).strip()
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                    pass
            return cid or "anonymous"

        def do_POST(self) -> None:
            """Обрабатывает входящие HTTP POST-запросы."""
            nonlocal cached_meta, last_client_activity
            parsed = urlparse(self.path)
            route = parsed.path

            if route == "/api/heartbeat":
                cid = self._extract_client_id(parsed.query)
                now = time.monotonic()
                with client_lock:
                    _prune_stale_clients_locked(now)
                    active_clients[cid] = now
                    last_client_activity = now
                    count = len(active_clients)
                self._send_json({"status": "ok", "active_clients": count})
                return

            if route == "/api/client/close":
                cid = self._extract_client_id(parsed.query)
                now = time.monotonic()
                with client_lock:
                    active_clients.pop(cid, None)
                    _prune_stale_clients_locked(now)
                    last_client_activity = now
                    count = len(active_clients)
                self._send_json({"status": "ok", "active_clients": count})
                return

            if route == "/api/shutdown":
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length > 0:
                    self.rfile.read(length)
                self._send_json({"status": "shutting_down"})
                threading.Thread(target=server.shutdown, daemon=True).start()
                return

            if route == "/api/open-input-dir":
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length > 0:
                    self.rfile.read(length)
                try:
                    _open_directory_in_file_manager(resolved_input)
                    self._send_json({"status": "ok", "path": str(resolved_input)})
                except OSError as exc:
                    logger.exception(
                        "Не удалось открыть папку input/: %s", resolved_input
                    )
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            if route == "/api/open-log-dir":
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length > 0:
                    self.rfile.read(length)
                try:
                    zip_path = save_and_reveal_diagnostic_zip(
                        db_path=resolved_db,
                        input_dir=resolved_input,
                        tmp_dir=resolved_tmp,
                        log_path=resolved_log,
                        reveal=True,
                    )
                    logger.info(
                        "Диагностический архив сохранён и показан в папке: %s", zip_path
                    )
                    self._send_json({"status": "ok", "path": str(zip_path)})
                except OSError as exc:
                    logger.exception(
                        "Не удалось сохранить или показать диагностический архив"
                    )
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            if route == "/api/client/error":
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = (
                    self.rfile.read(length).decode("utf-8", errors="replace")
                    if length > 0
                    else "{}"
                )
                try:
                    payload = json.loads(raw_body)
                    if not isinstance(payload, dict):
                        payload = {"message": str(payload)}
                except (ValueError, json.JSONDecodeError):
                    payload = {"message": raw_body}
                logger.error(
                    "JS Error в браузере: %s (%s:%s:%s) | url=%s | stack=%s",
                    payload.get("message", ""),
                    payload.get("source", ""),
                    payload.get("lineno", ""),
                    payload.get("colno", ""),
                    payload.get("url", ""),
                    payload.get("stack", ""),
                )
                self._send_json({"status": "ok"})
                return

            if route == "/api/import/cancel":
                with job_lock:
                    token = current_cancel_token
                    can_cancel = (
                        token is not None
                        and import_job["running"]
                        and import_job["phase"] in cancellable_phases
                    )
                    if can_cancel:
                        import_job["cancel_requested"] = True
                    cancel_file = import_job.get("file_name")
                if not can_cancel:
                    self._send_json(
                        {"error": "Сейчас нет импорта, который можно остановить"},
                        status=HTTPStatus.CONFLICT,
                    )
                    return
                logger.info("Запрошена остановка импорта файла %s", cancel_file)
                token.cancel()
                self._send_json({"status": "cancelling"})
                return

            if route == "/api/import/dismiss":
                with job_lock:
                    if not import_job["running"]:
                        import_job["phase"] = "idle"
                        import_job["error"] = None
                self._send_json({"status": "ok"})
                return

            if route == "/api/db/clear":
                try:
                    with job_lock:
                        if import_job["running"]:
                            self._send_json(
                                {
                                    "error": "Нельзя очистить базу данных во время выполнения импорта"
                                },
                                status=HTTPStatus.CONFLICT,
                            )
                            return
                        clear_db(resolved_db)
                        cached_meta = None
                        import_job.clear()
                        import_job.update(_make_import_job_state())
                    self._send_json({"status": "ok"})
                except (sqlite3.Error, OSError) as exc:
                    logger.exception("Ошибка при очистке БД (/api/db/clear)")
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            if route == "/api/import/start":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw_body = (
                        self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                    )
                    payload = json.loads(raw_body)
                    if not isinstance(payload, dict):
                        raise ValueError("Ожидался JSON-объект")
                    fname = Path(str(payload.get("file_name", ""))).name
                    src_date = payload.get("source_date")
                    if src_date is not None:
                        src_date = str(src_date).strip() or None
                    target = resolved_input / fname
                    if not fname or not target.is_file():
                        self._send_json(
                            {"error": f"Файл не найден в папке input/: {fname}"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    st = _start_background_import(target, src_date)
                    self._send_json(st)
                except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except RuntimeError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                except (sqlite3.Error, OSError) as exc:
                    logger.exception("Ошибка при запуске /api/import/start")
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            if route == "/api/import/upload":
                try:
                    with job_lock:
                        if import_job["running"]:
                            self._send_json(
                                {"error": "Импорт другого файла уже выполняется"},
                                status=HTTPStatus.CONFLICT,
                            )
                            return

                    qs = parse_qs(parsed.query, keep_blank_values=False)
                    raw_fname = unquote(_first_param(qs, "filename") or "uploaded.xlsx")
                    safe_fname = Path(raw_fname).name
                    src_date = _first_param(qs, "source_date") or None
                    content_length = int(self.headers.get("Content-Length", "0"))
                    if content_length <= 0:
                        empty_msg = (
                            f"Файл «{safe_fname}» пуст (0 байт). "
                            f"Скачайте полный файл выгрузки (.xlsx, .csv, .json или .xml)."
                        )
                        with job_lock:
                            import_job.clear()
                            import_job.update(
                                _make_import_job_state(
                                    running=False,
                                    phase="error",
                                    file_name=safe_fname,
                                    source_date=src_date,
                                    error=empty_msg,
                                )
                            )
                        self._send_json(
                            {"error": empty_msg},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return

                    with job_lock:
                        import_job.clear()
                        import_job.update(
                            _make_import_job_state(
                                running=True,
                                phase="uploading",
                                file_name=safe_fname,
                                source_date=src_date,
                                bytes_received=0,
                                bytes_total=content_length,
                            )
                        )

                    try:
                        logger.info(
                            "Загрузка файла в input/: имя=%s | размер=%d байт | дата=%s",
                            raw_fname,
                            content_length,
                            src_date or "авто",
                        )
                        saved_path, sha_hex, was_dup = save_uploaded_stream_to_input(
                            stream=self.rfile,
                            content_length=content_length,
                            raw_filename=raw_fname,
                            input_dir=resolved_input,
                            tmp_dir=resolved_tmp,
                            progress_cb=_update_progress,
                        )
                    except (ValueError, OSError) as exc:
                        with job_lock:
                            import_job["running"] = False
                            import_job["phase"] = "error"
                            import_job["error"] = str(exc)
                        raise

                    with job_lock:
                        import_job["running"] = False

                    st = _start_background_import(
                        target_file=saved_path,
                        src_date=src_date,
                        known_sha256=sha_hex,
                        was_dup=was_dup,
                        remove_on_validation_error=not was_dup,
                    )
                    self._send_json(st)
                except ValueError as exc:
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except RuntimeError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.CONFLICT)
                except (sqlite3.Error, OSError) as exc:
                    logger.exception("Ошибка при загрузке /api/import/upload")
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            self._send_json(
                {"error": f"Маршрут не найден: {route}"},
                status=HTTPStatus.NOT_FOUND,
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            """Записывает HTTP-запрос в логгер, пропуская успешные фоновые опросы и статику."""
            msg = fmt % args
            status_code = str(args[1]) if len(args) > 1 else ""
            req_path = urlparse(self.path).path
            if status_code == "200" and (
                req_path
                in (
                    "/api/heartbeat",
                    "/api/client/close",
                    "/api/import/status",
                    "/coat_of_arms.svg",
                    "/favicon.ico",
                )
                or req_path.startswith("/static/")
            ):
                return
            logger.info("%s - %s", self.address_string(), msg)

    class LocalLoopbackHTTPServer(ThreadingHTTPServer):
        """HTTP-сервер для loopback-интерфейса без вызова socket.getfqdn() (предотвращает mDNS-запрос macOS)."""

        def server_bind(self) -> None:
            """Привязывает сокет без DNS/Bonjour-резолвинга имени хоста."""
            socketserver.TCPServer.server_bind(self)
            host_addr, port_num = self.server_address[:2]
            self.server_name = str(host_addr)
            self.server_port = int(port_num)

    server = LocalLoopbackHTTPServer((host, port), GravesRequestHandler)

    if idle_timeout_sec > 0:
        check_interval = min(5.0, max(0.05, idle_timeout_sec / 2.0))

        def _idle_watchdog() -> None:
            while True:
                time.sleep(check_interval)
                try:
                    if server.socket.fileno() < 0:
                        return
                except OSError:
                    return
                with job_lock:
                    is_importing = bool(import_job["running"])
                now = time.monotonic()
                with client_lock:
                    _prune_stale_clients_locked(now)
                    no_clients = len(active_clients) == 0
                    idle_elapsed = now - last_client_activity
                if not is_importing and no_clients and idle_elapsed >= idle_timeout_sec:
                    logger.info(
                        "Нет активных вкладок более %.0f сек — автоматическая остановка сервера.",
                        idle_timeout_sec,
                    )
                    server.shutdown()
                    return

        threading.Thread(target=_idle_watchdog, daemon=True).start()

    return server
