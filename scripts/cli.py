"""Терминальный интерфейс (CLI) для импорта выгрузок и поиска захоронений."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import logging
import sys

from src.db import DEFAULT_DB_PATH, clear_db, migrate_legacy_db_file
from src.importer import import_file
from src.models import PersonFilter, SearchQuery
from src.parser import format_display_inscription
from src.search import search_plots

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """Создаёт и настраивает парсер аргументов командной строки.

    Returns:
        Настроенный экземпляр argparse.ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m scripts.cli",
        description="Поисковая система по архивным выгрузкам реестра захоронений Москвы (64023)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Команда import
    imp_parser = subparsers.add_parser(
        "import",
        help="Потоковый импорт одного или нескольких файлов (.xlsx, .json, .csv, .xml) в SQLite",
    )
    imp_parser.add_argument(
        "paths",
        nargs="+",
        help="Пути к импортируемым файлам выгрузок",
    )
    imp_parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Путь к файлу БД SQLite (по умолчанию: {DEFAULT_DB_PATH})",
    )
    imp_parser.add_argument(
        "--source-date",
        default=None,
        help="Явная дата актуальности данных YYYY-MM-DD (по умолчанию определяется автоматически)",
    )

    # Команда search
    srch_parser = subparsers.add_parser(
        "search",
        help="Поиск участков захоронений по ФИО, датам, кладбищу, участку или global_id",
    )
    srch_parser.add_argument(
        "fio_pos",
        nargs="?",
        default="",
        help="Строка поиска ФИО1 (опциональный позиционный аргумент)",
    )
    srch_parser.add_argument(
        "--fio",
        default="",
        help='Строка поиска ФИО1 (поддерживает кавычки "...", инициалы Е. Я., $, ?, ~, *, |)',
    )
    srch_parser.add_argument(
        "--mode",
        choices=("prefix_default", "prefix_no_surname", "substring"),
        default="prefix_default",
        help="Режим поиска ФИО1 (по умолчанию: prefix_default)",
    )
    srch_parser.add_argument(
        "--birth-exact",
        default=None,
        help="Точный год ('1954') или дата рождения ('16.03.1954') для ФИО1",
    )
    srch_parser.add_argument(
        "--birth-from",
        type=int,
        default=None,
        help="Год рождения «с» (включительно) для ФИО1",
    )
    srch_parser.add_argument(
        "--birth-to",
        type=int,
        default=None,
        help="Год рождения «по» (включительно) для ФИО1",
    )
    srch_parser.add_argument(
        "--death-exact",
        default=None,
        help="Точный год ('2015') или дата смерти ('18.12.2015') для ФИО1",
    )
    srch_parser.add_argument(
        "--death-from",
        type=int,
        default=None,
        help="Год смерти «с» (включительно) для ФИО1",
    )
    srch_parser.add_argument(
        "--death-to",
        type=int,
        default=None,
        help="Год смерти «по» (включительно) для ФИО1",
    )

    # Параметры второго человека на том же участке (ФИО2)
    srch_parser.add_argument(
        "--fio2",
        default="",
        help="Строка поиска второго человека на том же участке (ФИО2)",
    )
    srch_parser.add_argument(
        "--mode2",
        choices=("prefix_default", "prefix_no_surname", "substring"),
        default="prefix_default",
        help="Режим поиска ФИО2 (по умолчанию: prefix_default)",
    )
    srch_parser.add_argument(
        "--birth2-exact",
        default=None,
        help="Точный год или дата рождения для ФИО2",
    )
    srch_parser.add_argument(
        "--birth2-from",
        type=int,
        default=None,
        help="Год рождения «с» для ФИО2",
    )
    srch_parser.add_argument(
        "--birth2-to",
        type=int,
        default=None,
        help="Год рождения «по» для ФИО2",
    )
    srch_parser.add_argument(
        "--death2-exact",
        default=None,
        help="Точный год или дата смерти для ФИО2",
    )
    srch_parser.add_argument(
        "--death2-from",
        type=int,
        default=None,
        help="Год смерти «с» для ФИО2",
    )
    srch_parser.add_argument(
        "--death2-to",
        type=int,
        default=None,
        help="Год смерти «по» для ФИО2",
    )

    # Фильтры участка
    srch_parser.add_argument(
        "--cemetery",
        default=None,
        help="Фильтр по названию кладбища",
    )
    srch_parser.add_argument(
        "--cemetery-id",
        type=int,
        default=None,
        help="Фильтр по числовому ID кладбища",
    )
    srch_parser.add_argument(
        "--place",
        default=None,
        help="Фильтр по номеру участка (например, '3.3') или шифру места",
    )
    srch_parser.add_argument(
        "--global-id",
        type=int,
        default=None,
        help="Точный поиск по global_id участка",
    )
    srch_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Максимальное число выводимых участков (по умолчанию: 20)",
    )
    srch_parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Смещение результатов для пагинации (по умолчанию: 0)",
    )
    srch_parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Путь к файлу БД SQLite (по умолчанию: {DEFAULT_DB_PATH})",
    )
    srch_parser.add_argument(
        "--json",
        action="store_true",
        help="Вывести результаты поиска в формате JSON",
    )

    # Команда clear
    clr_parser = subparsers.add_parser(
        "clear",
        help="Очистить базу данных SQLite (исходные файлы в папке input/ сохраняются)",
    )
    clr_parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Путь к файлу БД SQLite (по умолчанию: {DEFAULT_DB_PATH})",
    )
    clr_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Не запрашивать подтверждение перед очисткой",
    )

    return parser


def _run_clear(args: argparse.Namespace) -> int:
    """Выполняет команду clear (очистка БД с подтверждением).

    Args:
        args: Разобранные аргументы командной строки.

    Returns:
        Код завершения процесса (0 — успешно).
    """
    if not args.yes and sys.stdin.isatty():
        ans = (
            input(
                f"Очистить базу данных {args.db!r}? Все импортированные записи будут удалены [y/N]: "
            )
            .strip()
            .lower()
        )
        if ans not in ("y", "yes", "д", "да"):
            logger.info("Очистка отменена.")
            return 0

    clear_db(args.db)
    logger.info("База данных %s очищена.", args.db)
    return 0


def _run_import(args: argparse.Namespace) -> int:
    """Выполняет команду import и логирует итоговую сводку.

    Args:
        args: Разобранные аргументы командной строки.

    Returns:
        Код завершения процесса (0 — успешно).
    """
    for file_path in args.paths:
        stats = import_file(
            file_path=file_path,
            db_path=args.db,
            source_date=args.source_date,
        )
        speed = stats.plots_seen / max(stats.elapsed_sec, 0.001)
        logger.info(
            "Импорт завершён: %s | дата=%s | участков=%d (новых=%d, обновл.=%d) | "
            "захоронений=%d (записано=%d) | всего в БД: %d уч. / %d надп. | %.2f с (%.0f уч/с)",
            stats.file_path,
            stats.source_date or "не указана",
            stats.plots_seen,
            stats.plots_inserted,
            stats.plots_updated,
            stats.persons_seen,
            stats.persons_inserted,
            stats.total_plots_in_db,
            stats.total_persons_in_db,
            stats.elapsed_sec,
            speed,
        )
    return 0


def _run_search(args: argparse.Namespace) -> int:
    """Выполняет команду search и печатает результаты в табличном или JSON-виде.

    Args:
        args: Разобранные аргументы командной строки.

    Returns:
        Код завершения процесса (0 — успешно).
    """
    fio1 = (args.fio or args.fio_pos or "").strip()
    p1 = PersonFilter(
        fio=fio1,
        search_mode=args.mode,
        birth_exact=args.birth_exact,
        birth_from=args.birth_from,
        birth_to=args.birth_to,
        death_exact=args.death_exact,
        death_from=args.death_from,
        death_to=args.death_to,
    )
    p2 = PersonFilter(
        fio=(args.fio2 or "").strip(),
        search_mode=args.mode2,
        birth_exact=args.birth2_exact,
        birth_from=args.birth2_from,
        birth_to=args.birth2_to,
        death_exact=args.death2_exact,
        death_from=args.death2_from,
        death_to=args.death2_to,
    )

    query = SearchQuery(
        person1=p1,
        person2=p2 if p2.is_active() else None,
        cemetery_id=args.cemetery_id,
        cemetery_name=args.cemetery,
        place_number=args.place,
        global_id=args.global_id,
        limit=args.limit,
        offset=args.offset,
    )

    try:
        resp = search_plots(db_path=args.db, query=query)
    except ValueError as exc:
        logger.info("Некорректный поисковый запрос: %s", exc)
        return 1

    if args.json:
        print(json.dumps(asdict(resp), ensure_ascii=False, indent=2))
        return 0

    more_suffix = "+" if resp.has_more else ""
    print(
        f"Найдено участков: {len(resp.results)} "
        f"(показаны {args.offset + 1}..{args.offset + len(resp.results)} "
        f"из {resp.total_plots}{more_suffix}) за {resp.elapsed_ms:.2f} мс\n"
    )

    for idx, plot in enumerate(resp.results, start=args.offset + 1):
        place_info = f" | Шифр: {plot.place_number}" if plot.place_number else ""
        sec_info = f" | Уч.: {plot.plot_section}" if plot.plot_section else ""
        area_info = f" | Площадь: {plot.area} м²" if plot.area is not None else ""
        coords_info = (
            f" | Коорд.: {plot.lat}, {plot.lon}"
            if plot.lat is not None and plot.lon is not None
            else ""
        )
        print(
            f"[{idx}] global_id={plot.global_id} | {plot.cemetery_name} "
            f"({plot.grave_type_label})"
            f"{sec_info}{place_info}{area_info}{coords_info}"
        )
        for p in plot.persons:
            line_disp = format_display_inscription(
                p.raw_line,
                p.birth_date_raw,
                p.death_date_raw,
                p.single_date_raw,
            )
            if p.matched_p1:
                print(f"  \033[1;34m{line_disp}\033[0m")
            elif p.matched_p2:
                print(f"  \033[1;33m{line_disp}\033[0m")
            else:
                print(f"  {line_disp}")
        if plot.epoisk_map_url:
            print(f"    Схема epoisk : {plot.epoisk_map_url}")
        if plot.ya_url:
            print(f"    Яндекс.Карты : {plot.ya_url}")
        print()

    return 0


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI.

    Args:
        argv: Список аргументов командной строки (или None для sys.argv).

    Returns:
        Код завершения процесса.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "db", None) == DEFAULT_DB_PATH:
        args.db = str(migrate_legacy_db_file(DEFAULT_DB_PATH))

    if args.command == "import":
        return _run_import(args)
    if args.command == "search":
        return _run_search(args)
    if args.command == "clear":
        return _run_clear(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
