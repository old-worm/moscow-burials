"""Тесты парсера, потокового импорта, идемпотентного слияния, поиска FTS5 и веб-API."""

from __future__ import annotations

import io
import json
import logging
import shutil
import sqlite3
from logging.handlers import RotatingFileHandler
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from scripts.web import _is_our_server_running, resolve_app_data_dirs
from src.db import clear_db, migrate_legacy_db_file
from src.diag import save_and_reveal_diagnostic_zip, setup_app_logging
from src.importer import (
    DataValidationError,
    ImportCancelledError,
    ImportCancelToken,
    detect_source_date,
    import_file,
    list_input_sources,
    save_uploaded_stream_to_input,
    validate_source_file,
)
from src.models import PersonFilter, SearchQuery
from src.parser import (
    build_urls,
    deduplicate_inscriptions,
    extract_geo_info,
    format_display_inscription,
    parse_inscription_line,
    parse_place_number,
    split_display_inscription,
)
from src.search import (
    _strip_flexion,
    get_db_meta,
    parse_date_filter_expr,
    search_plots,
)
from src import __version__
from src.web_app import create_http_server


class ParserEdgeCasesTest(unittest.TestCase):
    """Проверка краевых случаев и аномалий входных данных из SPEC.md."""

    def test_commas_inside_fio_right_to_left(self) -> None:
        """Запятые внутри ФИО, инициалов и скобок не должны ломать разбор дат."""
        p1 = parse_inscription_line("Ларькина С.М,,год 1919,год 1998")
        self.assertEqual(p1.surname, "ларькина")
        self.assertEqual(p1.firstname, "с")
        self.assertEqual(p1.patronymic, "м")
        self.assertEqual(p1.birth_year, 1919)
        self.assertEqual(p1.death_year, 1998)

        p2 = parse_inscription_line("Попов И,А.,год 1931,год 1987")
        self.assertEqual(p2.surname, "попов")
        self.assertEqual(p2.firstname, "и")
        self.assertEqual(p2.patronymic, "а")
        self.assertEqual(p2.birth_year, 1931)
        self.assertEqual(p2.death_year, 1987)

        p3 = parse_inscription_line("Чернов Т. Т.(Сергей, сын),30.09.1931,17.03.1992")
        self.assertEqual(p3.surname, "чернов")
        self.assertIn("сергей", p3.fio_norm)
        self.assertEqual(p3.birth_date_raw, "30.09.1931")
        self.assertEqual(p3.death_date_raw, "17.03.1992")

        p4 = parse_inscription_line("Участок по разрешению ДТиУ №РД/002/3,2,2/0002")
        self.assertIsNone(p4.birth_year)
        self.assertIsNone(p4.death_year)
        self.assertIsNone(p4.single_date_year)
        self.assertIn("3,2,2", p4.fio_norm)

    def test_multi_token_and_damaged_dates(self) -> None:
        """Разбор двухтокенных дат, пустых ФИО и повреждённых годов ('год **28')."""
        p1 = parse_inscription_line(
            "Иванов Пётр Сидорович,месяц 07,год 1896,месяц 05,год 1970"
        )
        self.assertEqual(p1.firstname, "петр")
        self.assertEqual(p1.birth_date_raw, "месяц 07,год 1896")
        self.assertEqual(p1.birth_year, 1896)
        self.assertEqual(p1.death_date_raw, "месяц 05,год 1970")
        self.assertEqual(p1.death_year, 1970)

        p2 = parse_inscription_line(",день 16,месяц 08,12.11.2018")
        self.assertEqual(p2.fio_norm, "")
        self.assertIsNone(p2.surname)
        self.assertEqual(p2.birth_date_raw, "день 16,месяц 08")
        self.assertIsNone(p2.birth_year)
        self.assertEqual(p2.death_date_raw, "12.11.2018")
        self.assertEqual(p2.death_year, 2018)

        p3 = parse_inscription_line("Смирнов А.А.,год **28,год 1995")
        self.assertEqual(p3.birth_date_raw, "год **28")
        self.assertIsNone(p3.birth_year)
        self.assertEqual(p3.death_year, 1995)

        p4 = parse_inscription_line("Щукина Людмила Александровна,16.03.1954")
        self.assertIsNone(p4.birth_year)
        self.assertIsNone(p4.death_year)
        self.assertEqual(p4.single_date_year, 1954)
        self.assertEqual(p4.single_date_raw, "16.03.1954")

    def test_place_number_parsing(self) -> None:
        """Разбор 4-, 5- и 7-сегментных шифров PlaceNumber."""
        self.assertEqual(parse_place_number("ЗИ/002/3.3/1667"), ("002", "3.3"))
        self.assertEqual(parse_place_number("ЗИ/016/14/2/0105"), ("016", "14/2"))
        self.assertEqual(parse_place_number("КО/078/0002/24/3/3/13"), ("078", "0002"))

    def test_prefix_deduplication(self) -> None:
        """Префиксная регистронезависимая дедупликация надписей на одном участке."""
        raw = [
            "Щукина Людмила Александровна,16.03.1954,18.12.2015",
            "Щукина Людмила Александровна,16.03.1954",
            "ЧЕМАГИН ЮРИЙ КОНСТАНТИНОВИЧ,23.08.1940",
            "Чемагин Юрий Константинович,23.08.1940,14.05.2016",
        ]
        dedup = deduplicate_inscriptions(raw)
        self.assertEqual(
            dedup,
            [
                "Щукина Людмила Александровна,16.03.1954,18.12.2015",
                "Чемагин Юрий Константинович,23.08.1940,14.05.2016",
            ],
        )


class ImportMergeAndSearchTest(unittest.TestCase):
    """Интеграционная проверка слияния источников, логики epoisk.ru/burmsk/ и веб-сервера."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.db_path = self.root / "test_burials.db"

        self.csv_path = self.root / "dump.csv"
        self.csv_path.write_text(
            "PlaceNumber;GraveType;HasTombstone;Inscription;Cemetery;global_id\n"
            "Номер места;Тип;Наличие;Надпись;Кладбище;global_id\n"
            '"ЗИ/002/3.3/1667";"урной в землю";"да";'
            '"Inscription:Щукина Людмила Александровна,16.03.1954,18.12.2015\n\n'
            'Inscription:Щукина Людмила Александровна,16.03.1954\n\n";"Алексеевское";"2606331479"\n'
            '"ЗИ/002/3.2/1219";"урной в землю";"да";'
            '"Inscription:Введенский Михаил Николаевич,27.11.1924,28.12.2022\n\n'
            'Inscription:Введенский Евгений михайлович,26.09.1950,16.08.2016\n\n";"Алексеевское";"2606331495"\n'
            '"ЗИ/002/3.6/2000";"урной в землю";"да";'
            '"Inscription:Кузмиченков Алексей Васильевич,18.10.1923,04.11.2016\n\n'
            'Inscription:Кузьмиченков Алексей Васильевич,18.10.1923\n\n";"Алексеевское";"2606332552"\n'
            '"ЗИ/002/1.1/0001";"гробом в землю";"да";'
            '"Inscription:Красновский Евгений Яковлевич,год 1910,год 1980\n\n";"Алексеевское";"2606339001"\n'
            '"ЗИ/002/1.1/0002";"гробом в землю";"да";'
            '"Inscription:Красновский Е Я,год 1920,год 1990\n\n";"Алексеевское";"2606339002"\n'
            '"ЗИ/002/1.1/0003";"гробом в землю";"да";'
            '"Inscription:Красновская Е. И.,год 1915,год 1995\n\n";"Алексеевское";"2606339003"\n'
            '"ЗИ/002/1.1/0004";"гробом в землю";"да";'
            '"Inscription:Иванов Иван Иванович,год 1900,год 1970\n\n";"Алексеевское";"2606339004"\n'
            '"ЗИ/002/1.1/0005";"гробом в землю";"да";'
            '"Inscription:Петров Ивана Сергеевич,год 1905,год 1975\n\n";"Алексеевское";"2606339005"\n'
            '"ЗИ/002/1.1/0006";"гробом в землю";"да";'
            '"Inscription:Соколов-Микитов Сергей Петрович,год 1892,год 1965\n\n";"Алексеевское";"2606339006"\n',
            encoding="utf-8-sig",
        )

        self.json_path = self.root / "dump.json"
        self.json_path.write_text(
            "[\n"
            + json.dumps(
                {
                    "global_id": 2606331479,
                    "Cemetery": "Алексеевское",
                    "GraveType": "урной в землю",
                    "HasTombstone": "да",
                    "Inscription": [
                        {
                            "Inscription": "Щукина Людмила Александровна,16.03.1954,18.12.2015"
                        }
                    ],
                    "geoData": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [37.646255936, 55.817816769],
                                [37.646258643, 55.817824005],
                                [37.64627797, 55.817821853],
                                [37.646275566, 55.817814509],
                                [37.646255936, 55.817816769],
                            ]
                        ],
                    },
                },
                ensure_ascii=False,
            )
            + "\n,\n"
            + json.dumps(
                {
                    "global_id": 2635555555,
                    "Number": 2,
                    "Cells": {
                        "global_id": 2635555555,
                        "Cemetery": "Ваганьковское",
                        "GraveType": "гробом в землю",
                        "HasTombstone": "да",
                        "Inscription": [
                            {
                                "Inscription": "Соколова Галина Васильевна,03.03.1917,27.11.2002"
                            }
                        ],
                        "geodata_center": {
                            "type": "Point",
                            "coordinates": [37.549281, 55.769894],
                        },
                    },
                },
                ensure_ascii=False,
            )
            + "\n]\n",
            encoding="utf-8",
        )

        import_file(self.csv_path, self.db_path)
        import_file(self.json_path, self.db_path)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_idempotent_upsert_merge(self) -> None:
        """Слияние табличной выгрузки и JSON-выгрузки data.mos.ru сохраняет и шифр, и координаты."""
        resp = search_plots(self.db_path, SearchQuery(global_id=2606331479))
        self.assertEqual(len(resp.results), 1)
        plot = resp.results[0]
        self.assertEqual(plot.place_number, "ЗИ/002/3.3/1667")
        self.assertEqual(plot.plot_section, "3.3")
        self.assertEqual(plot.lon, 37.646267)
        self.assertEqual(plot.lat, 55.817819)
        self.assertEqual(plot.area, 1.03)
        self.assertEqual(
            plot.ya_url,
            "https://yandex.ru/maps/?ll=37.646267%2C55.817819&lang=ru&mode=search&text=55.817819%2C37.646267&z=17",
        )
        self.assertEqual(
            plot.epoisk_map_url,
            "https://epoisk.ru/burmsk/cemetery/alekseevskoe/map/?gid=2606331479",
        )
        self.assertEqual(
            plot.google_url,
            "https://www.google.com/maps?q=55.817819,37.646267&ll=55.817819,37.646267&z=18",
        )
        self.assertEqual(
            plot.osm_url,
            "https://www.openstreetmap.org/?mlat=55.817819&mlon=37.646267#map=19/55.817819/37.646267",
        )
        self.assertEqual(
            plot.nakarte_url,
            "https://nakarte.me/#m=18/55.817819/37.646267&l=I",
        )
        self.assertFalse(hasattr(plot, "dmr_url"))
        self.assertEqual(
            build_urls(2606331479, 37.646267, 55.817819),
            (
                "https://yandex.ru/maps/?ll=37.646267%2C55.817819&lang=ru&mode=search&text=55.817819%2C37.646267&z=17",
                "https://www.google.com/maps?q=55.817819,37.646267&ll=55.817819,37.646267&z=18",
            ),
        )
        self.assertEqual(build_urls(2606331479, None, None), (None, None))
        self.assertEqual(len(plot.persons), 1)
        self.assertEqual(plot.persons[0].fio_display, "Щукина Людмила Александровна")
        self.assertEqual(plot.persons[0].dates_display, "16.03.1954 – 18.12.2015")

        stats = import_file(self.csv_path, self.db_path)
        self.assertEqual(stats.total_plots_in_db, 10)

    def test_epoisk_rules_endings_quotes_initials_and_operators(self) -> None:
        """Полная проверка правил поиска epoisk.ru/burmsk/."""
        # 1. По запросу 'Красновский' находятся и 'Красновский', и 'Красновская'
        r_all_kras = search_plots(
            self.db_path, SearchQuery(person1=PersonFilter(fio="Красновский"))
        )
        self.assertEqual(r_all_kras.total_plots, 3)

        # 2. Кавычки '"Красновский"' — только мужская форма (без 'Красновская')
        r_exact_kras = search_plots(
            self.db_path, SearchQuery(person1=PersonFilter(fio='"Красновский"'))
        )
        self.assertEqual(r_exact_kras.total_plots, 2)
        for pl in r_exact_kras.results:
            self.assertNotEqual(pl.global_id, 2606339003)

        # 3. Кавычки + точные инициалы с точками: '"Красновская" Е. И.'
        r_kras_fem = search_plots(
            self.db_path,
            SearchQuery(person1=PersonFilter(fio='"Красновская" Е. И.')),
        )
        self.assertEqual(r_kras_fem.total_plots, 1)
        self.assertEqual(r_kras_fem.results[0].global_id, 2606339003)

        # 4. Точные инициалы с точками: 'Красновский Е. Я.' (не находит 'Евгений Яковлевич')
        r_init_dots = search_plots(
            self.db_path,
            SearchQuery(person1=PersonFilter(fio="Красновский Е. Я.")),
        )
        self.assertEqual(r_init_dots.total_plots, 1)
        self.assertEqual(r_init_dots.results[0].global_id, 2606339002)

        # 5. Инициалы без точек: 'Красновский Е Я' (находит и полные формы, и инициалы)
        r_init_nodots = search_plots(
            self.db_path,
            SearchQuery(person1=PersonFilter(fio="Красновский Е Я")),
        )
        self.assertEqual(r_init_nodots.total_plots, 2)

        # 6. Фамилия и отчество (2 слова без имени): 'Красновский Яковлевич'
        r_sur_pat = search_plots(
            self.db_path,
            SearchQuery(person1=PersonFilter(fio="Красновский Яковлевич")),
        )
        self.assertEqual(r_sur_pat.total_plots, 1)
        self.assertEqual(r_sur_pat.results[0].global_id, 2606339001)

        # 7. Части слов: 'Красновс Евген Яковлев'
        r_stems = search_plots(
            self.db_path,
            SearchQuery(person1=PersonFilter(fio="Красновс Евген Яковлев")),
        )
        self.assertEqual(r_stems.total_plots, 1)
        self.assertEqual(r_stems.results[0].global_id, 2606339001)

        # 8. Символ '$' (конец слова): 'Иван$' в firstname находит 'Иван', но не 'Ивана'
        r_dollar = search_plots(
            self.db_path,
            SearchQuery(
                person1=PersonFilter(fio="Иван$", search_mode="prefix_no_surname")
            ),
        )
        self.assertEqual(r_dollar.total_plots, 1)
        self.assertEqual(r_dollar.results[0].global_id, 2606339004)

        # 9. Символы '~' и '$': 'Иван~$' находит 'Иван' и 'Ивана', но не 'Иванов'
        r_tilde_dollar = search_plots(
            self.db_path,
            SearchQuery(
                person1=PersonFilter(fio="Иван~$", search_mode="prefix_no_surname")
            ),
        )
        self.assertEqual(r_tilde_dollar.total_plots, 2)

        # 10. Оператор '~' внутри слова: 'Куз~миченков' находит и Кузмиченков, и Кузьмиченков
        r_tilde_mid = search_plots(
            self.db_path,
            SearchQuery(person1=PersonFilter(fio="Куз~миченков")),
        )
        self.assertEqual(r_tilde_mid.total_plots, 1)
        self.assertTrue(r_tilde_mid.results[0].persons[0].matched_p1)
        self.assertTrue(r_tilde_mid.results[0].persons[1].matched_p1)

        # 11. Совместное захоронение ФИО1 + ФИО2
        r_co = search_plots(
            self.db_path,
            SearchQuery(
                person1=PersonFilter(fio="Введенский Михаил"),
                person2=PersonFilter(fio="Введенский Евгений"),
            ),
        )
        self.assertEqual(r_co.total_plots, 1)
        self.assertEqual(r_co.results[0].global_id, 2606331495)

        # 12. Точное отсечение только родовых окончаний фамилий в именительном падеже
        self.assertEqual(_strip_flexion("красновский"), "красновск")
        self.assertEqual(_strip_flexion("красновская"), "красновск")
        self.assertEqual(_strip_flexion("соколова"), "соколов")
        self.assertEqual(_strip_flexion("щукина"), "щукин")
        # Имена и несклоняемые фамилии не урезаются
        self.assertEqual(_strip_flexion("мария"), "мария")
        self.assertEqual(_strip_flexion("илья"), "илья")
        self.assertEqual(_strip_flexion("евгений"), "евгений")
        self.assertEqual(_strip_flexion("дмитрий"), "дмитрий")
        self.assertEqual(_strip_flexion("галина"), "галина")
        self.assertEqual(_strip_flexion("марина"), "марина")
        self.assertEqual(_strip_flexion("шевченко"), "шевченко")
        self.assertEqual(_strip_flexion("дюма"), "дюма")

        # 12.5. Дефисная фамилия со звёздочкой на конце: 'соколов-микитов*' (без FTS5 syntax error)
        r_hyph_star = search_plots(
            self.db_path,
            SearchQuery(person1=PersonFilter(fio="соколов-микитов*")),
        )
        self.assertEqual(r_hyph_star.total_plots, 1)
        self.assertEqual(r_hyph_star.results[0].global_id, 2606339006)

        # 13. Поиск по единому полю дат: ММ.ГГГГ, ДД.ММ.ГГГГ, смешанные диапазоны и учёт записей только с годом
        # Поиск по месяцу и году: 03.1954 находит Щукину (16.03.1954)
        r_month_year = search_plots(
            self.db_path, SearchQuery(person1=PersonFilter(birth_exact="03.1954"))
        )
        self.assertEqual(r_month_year.total_plots, 1)
        self.assertEqual(r_month_year.results[0].global_id, 2606331479)

        # Диапазон 1915-01.03.1954: включает записи только с годом (1915, 1920, 1923, 1950)
        # и Соколову (03.03.1917), но отсекает Щукину (16.03.1954 > 01.03.1954)
        r_range_day = search_plots(
            self.db_path,
            SearchQuery(person1=PersonFilter(birth_exact="1915-01.03.1954")),
        )
        gids_day = {pl.global_id for pl in r_range_day.results}
        self.assertIn(2606339003, gids_day)  # год 1915 (только год)
        self.assertIn(2606339002, gids_day)  # год 1920 (только год)
        self.assertIn(2635555555, gids_day)  # 03.03.1917
        self.assertNotIn(2606331479, gids_day)  # 16.03.1954 > 01.03.1954

        # Если же в 1954 году у человека известен только год (год 1954), он должен включаться в 1915-01.03.1954
        # Проверим на диапазоне смерти 1970-10.08.2016:
        # Запись 'год 1970' (2606339004), 'год 1975', 'год 1980', 'год 1990', 'год 1995' включаются;
        # Введенский Евгений (16.08.2016 > 10.08.2016) и Кузмиченков (04.11.2016 > 10.08.2016) исключаются.
        r_death_range = search_plots(
            self.db_path,
            SearchQuery(person1=PersonFilter(death_exact="15.12.1970-10.08.2016")),
        )
        death_gids = {pl.global_id for pl in r_death_range.results}
        self.assertIn(
            2606339004, death_gids
        )  # смерть 'год 1970' включена несмотря на нижнюю границу 15.12.1970!
        self.assertIn(2606331479, death_gids)  # 18.12.2015
        self.assertNotIn(2606332552, death_gids)  # 04.11.2016 > 10.08.2016

        # Валидация некорректных дат и диапазонов
        for invalid_expr in (
            "31.02.1967",
            "13.1967",
            "1970-1950",
            "15.12.1967-10.12.1967",
            "05.1967-02.1967",
            "1967-12-12",
            "-",
        ):
            with self.assertRaises(
                ValueError, msg=f"Ожидалась ошибка для {invalid_expr!r}"
            ):
                parse_date_filter_expr(invalid_expr)

    def test_http_api_and_csv_export(self) -> None:
        """Проверка эндпоинтов /api/meta, /api/search и /api/export.csv."""
        srv = create_http_server(self.db_path, host="127.0.0.1", port=0)
        host, port = srv.server_address[:2]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            base = f"http://{host}:{port}"
            with urllib.request.urlopen(f"{base}/") as resp_idx:
                self.assertEqual(resp_idx.status, 200)
                self.assertIn("text/html", resp_idx.headers.get("Content-Type", ""))
                idx_html = resp_idx.read().decode("utf-8")
                self.assertIn("/static/styles.css", idx_html)
                self.assertIn("/static/app.js", idx_html)
                self.assertIn(f"версия {__version__}", idx_html)
                self.assertNotIn("{{APP_VERSION}}", idx_html)

            with urllib.request.urlopen(f"{base}/static/styles.css") as resp_css:
                self.assertEqual(resp_css.status, 200)
                self.assertIn("text/css", resp_css.headers.get("Content-Type", ""))
                self.assertIn(".results-table", resp_css.read().decode("utf-8"))

            with urllib.request.urlopen(f"{base}/static/app.js") as resp_js:
                self.assertEqual(resp_js.status, 200)
                self.assertIn(
                    "application/javascript", resp_js.headers.get("Content-Type", "")
                )
                js_body = resp_js.read().decode("utf-8")
                self.assertIn("function splitPersonDisplay(p)", js_body)
                self.assertNotIn("DATE_TOKEN_JS_RE", js_body)

            meta = json.loads(
                urllib.request.urlopen(f"{base}/api/meta").read().decode("utf-8")
            )
            self.assertEqual(meta["total_plots"], 10)

            q = urllib.parse.urlencode({"fio1": "Щукина"})
            srch = json.loads(
                urllib.request.urlopen(f"{base}/api/search?{q}").read().decode("utf-8")
            )
            self.assertEqual(srch["total_plots"], 1)
            self.assertEqual(
                srch["results"][0]["persons"][0]["fio_display"],
                "Щукина Людмила Александровна",
            )
            self.assertEqual(
                srch["results"][0]["persons"][0]["dates_display"],
                "16.03.1954 – 18.12.2015",
            )

            with urllib.request.urlopen(f"{base}/api/export.csv?{q}") as resp_csv:
                disposition = resp_csv.headers.get("Content-Disposition", "")
                csv_raw = resp_csv.read().decode("utf-8-sig")
            self.assertRegex(
                disposition,
                r'^attachment; filename="burials-export-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.csv"$',
            )
            self.assertIn("2606331479", csv_raw)
            self.assertIn(
                "Щукина Людмила Александровна (16.03.1954 – 18.12.2015)", csv_raw
            )

            self.assertEqual(
                split_display_inscription(
                    "Астахов Петр Семенович,год 1894,год 1951",
                    "год 1894",
                    "год 1951",
                    None,
                ),
                ("Астахов Петр Семенович", "1894 – 1951"),
            )
            self.assertEqual(
                format_display_inscription(
                    "Астахов Петр Семенович,год 1894,год 1951",
                    "год 1894",
                    "год 1951",
                    None,
                ),
                "Астахов Петр Семенович (1894 – 1951)",
            )
            self.assertEqual(
                format_display_inscription(
                    ",день 16,месяц 08,12.11.2018",
                    "день 16,месяц 08",
                    "12.11.2018",
                    None,
                ),
                "ФИО не указано (16.08.**** – 12.11.2018)",
            )
            self.assertEqual(
                split_display_inscription(
                    "Захарова Ольга,месяц 06,год 1897,месяц 05,год 1976",
                    "месяц 06,год 1897",
                    "месяц 05,год 1976",
                    None,
                ),
                ("Захарова Ольга", "06.1897 – 05.1976"),
            )
            self.assertEqual(
                split_display_inscription(
                    "Шиконов Борис Иванович,15.02.1949,день 17,год 2004",
                    "15.02.1949",
                    "день 17,год 2004",
                    None,
                ),
                ("Шиконов Борис Иванович", "15.02.1949 – 17.**.2004"),
            )
            self.assertEqual(
                split_display_inscription(
                    "Ларькина С.М,,год 1919,год 1998",
                    "год 1919",
                    "год 1998",
                    None,
                ),
                ("Ларькина С.М", "1919 – 1998"),
            )

            # Проверка /api/sources (наличие input_dir и db_path)
            sources_info = json.loads(
                urllib.request.urlopen(f"{base}/api/sources").read().decode("utf-8")
            )
            self.assertIn("input_dir", sources_info)
            self.assertIn("db_path", sources_info)
            self.assertTrue(sources_info["input_dir"].endswith("input"))

            # Проверка /api/heartbeat и /api/client/close
            hb_req1 = urllib.request.Request(
                f"{base}/api/heartbeat?client_id=tab-1", method="POST"
            )
            hb_resp1 = json.loads(
                urllib.request.urlopen(hb_req1).read().decode("utf-8")
            )
            self.assertEqual(hb_resp1["status"], "ok")
            self.assertEqual(hb_resp1["active_clients"], 1)

            hb_req2 = urllib.request.Request(
                f"{base}/api/heartbeat",
                data=json.dumps({"client_id": "tab-2"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            hb_resp2 = json.loads(
                urllib.request.urlopen(hb_req2).read().decode("utf-8")
            )
            self.assertEqual(hb_resp2["status"], "ok")
            self.assertEqual(hb_resp2["active_clients"], 2)

            close_req = urllib.request.Request(
                f"{base}/api/client/close?client_id=tab-1", method="POST"
            )
            close_resp = json.loads(
                urllib.request.urlopen(close_req).read().decode("utf-8")
            )
            self.assertEqual(close_resp["status"], "ok")
            self.assertEqual(close_resp["active_clients"], 1)

            # Проверка _is_our_server_running на живом сервере
            self.assertTrue(_is_our_server_running(host, port))
            self.assertFalse(_is_our_server_running(host, 0))

            # Проверка POST /api/client/error, POST /api/open-log-dir и GET /api/log.zip
            log_file = self.db_path.parent / "logs" / "app.log"
            setup_app_logging(log_file)
            try:
                err_req = urllib.request.Request(
                    f"{base}/api/client/error",
                    data=json.dumps(
                        {
                            "message": "Test frontend error",
                            "source": "/static/app.js",
                            "lineno": 42,
                            "colno": 7,
                            "stack": "Error: Test frontend error",
                            "url": f"{base}/",
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                err_resp = json.loads(
                    urllib.request.urlopen(err_req).read().decode("utf-8")
                )
                self.assertEqual(err_resp["status"], "ok")

                with patch("src.diag.reveal_path_in_file_manager") as mock_reveal:
                    open_log_req = urllib.request.Request(
                        f"{base}/api/open-log-dir", method="POST"
                    )
                    open_log_resp = json.loads(
                        urllib.request.urlopen(open_log_req).read().decode("utf-8")
                    )
                    self.assertEqual(open_log_resp["status"], "ok")
                    self.assertTrue(
                        Path(open_log_resp["path"])
                        .as_posix()
                        .endswith("logs/moscow-burials-diagnostic.zip")
                    )
                    self.assertTrue(Path(open_log_resp["path"]).is_file())
                    mock_reveal.assert_called_once()

                with urllib.request.urlopen(f"{base}/api/log.zip") as zip_resp:
                    self.assertEqual(zip_resp.status, 200)
                    self.assertEqual(
                        zip_resp.headers.get("Content-Type"), "application/zip"
                    )
                    self.assertIn(
                        "moscow-burials-diagnostic.zip",
                        zip_resp.headers.get("Content-Disposition", ""),
                    )
                    zip_bytes = zip_resp.read()

                with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                    names = zf.namelist()
                    self.assertIn("diagnostic_info.json", names)
                    self.assertIn("app.log", names)
                    diag_info = json.loads(
                        zf.read("diagnostic_info.json").decode("utf-8")
                    )
                    self.assertEqual(diag_info["database"]["total_plots"], 10)
                    log_text = zf.read("app.log").decode("utf-8", errors="replace")
                    self.assertIn("Test frontend error", log_text)
            finally:
                root_logger = logging.getLogger()
                for h in list(root_logger.handlers):
                    if isinstance(h, RotatingFileHandler):
                        root_logger.removeHandler(h)
                        h.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_resolve_app_data_dirs(self) -> None:
        """Проверка разрешения путей данных в dev-режиме и в собранном пакете (macOS/Windows)."""
        db_dev, in_dev, tmp_dev = resolve_app_data_dirs()
        self.assertTrue(str(db_dev).endswith("burials.db"))
        self.assertEqual(in_dev.name, "input")
        self.assertEqual(tmp_dev.name, "tmp")

        with patch("scripts.web.sys.frozen", True, create=True), patch(
            "scripts.web.sys.platform", "darwin"
        ):
            db_mac, in_mac, tmp_mac = resolve_app_data_dirs()
            self.assertIn(
                "Library/Application Support/moscow-burials", db_mac.as_posix()
            )
            self.assertIn(
                "Library/Application Support/moscow-burials/input", in_mac.as_posix()
            )
            self.assertIn(
                "Library/Application Support/moscow-burials/tmp", tmp_mac.as_posix()
            )

        fake_exe = self.root / "win_portable" / "moscow-burials.exe"
        fake_exe.parent.mkdir(parents=True, exist_ok=True)
        with patch("scripts.web.sys.frozen", True, create=True), patch(
            "scripts.web.sys.platform", "win32"
        ), patch("scripts.web.sys.executable", str(fake_exe)):
            db_win, in_win, tmp_win = resolve_app_data_dirs()
            self.assertEqual(
                db_win, (fake_exe.parent / "output" / "burials.db").resolve()
            )
            self.assertEqual(in_win, (fake_exe.parent / "input").resolve())
            self.assertEqual(tmp_win, (fake_exe.parent / "tmp").resolve())

        with patch("scripts.web.sys.frozen", True, create=True), patch(
            "scripts.web.sys.platform", "linux"
        ), patch(
            "scripts.web.sys.executable", "/opt/moscow-burials/moscow-burials"
        ), patch(
            "scripts.web._is_dir_writable", return_value=False
        ), patch.dict(
            "scripts.web.os.environ",
            {"XDG_DATA_HOME": str(self.root / "xdg_data")},
            clear=False,
        ):
            db_lin, in_lin, tmp_lin = resolve_app_data_dirs()
            self.assertEqual(
                db_lin,
                (
                    self.root / "xdg_data" / "moscow-burials" / "output" / "burials.db"
                ).resolve(),
            )
            self.assertEqual(
                in_lin,
                (self.root / "xdg_data" / "moscow-burials" / "input").resolve(),
            )
            self.assertEqual(
                tmp_lin,
                (self.root / "xdg_data" / "moscow-burials" / "tmp").resolve(),
            )

    def test_freshness_completeness_merge_and_dedup_upload(self) -> None:
        """Проверка правил 'Свежесть + Полнота', парсинга geoData Apache POI и защиты от дубликатов."""
        # 1. Парсинг строкового geoData Apache POI из XLSX 2024 года
        poi_geo = (
            "{coordinates=[[[37.646255936, 55.817816769], "
            "[37.646258643, 55.817824005], [37.64627797, 55.817821853], "
            "[37.646275566, 55.817814509], [37.646255936, 55.817816769]]], "
            "type=Polygon}"
        )
        lon, lat, area = extract_geo_info(poi_geo)
        self.assertEqual((lon, lat, area), (37.646267, 55.817819, 1.03))

        # 2. Определение даты из имени файла и игнорирование заглушки 2999-01-01
        self.assertIsNone(detect_source_date("data-64023-2999-01-01.csv"))
        self.assertEqual(
            detect_source_date("Реестр (версия от 19.03.2024).csv"),
            "2024-03-19",
        )

        # 3. Слияние по дате актуальности и полноте:
        # Загружаем свежий срез 2024-03-19 с исправленной надписью для 2606331479
        fresh_db = self.root / "fresh.db"
        csv_2023 = self.root / "dump_2023-11-16.csv"
        csv_2023.write_text(
            "PlaceNumber;GraveType;HasTombstone;Inscription;Cemetery;global_id\n"
            '"ЗИ/002/2.3/2451";"гробом в землю";"да";'
            '"Inscription:Старая Ошибочная Запись,год 1950,год 2010\n\n";"Алексеевское";"2606338129"\n',
            encoding="utf-8",
        )
        csv_2024 = self.root / "dump_19.03.2024.csv"
        csv_2024.write_text(
            "PlaceNumber;GraveType;HasTombstone;Inscription;Cemetery;global_id;geoData\n"
            f'"";"гробом в землю";"да";'
            f'"Inscription:Белоцкая Светлана Николаевна,05.06.1959,08.11.2023\n\n";'
            f'"Алексеевское";"2606338129";"{poi_geo}"\n',
            encoding="utf-8",
        )

        # Сначала старый (2023), затем свежий (2024, но без PlaceNumber)
        st_2023 = import_file(csv_2023, fresh_db)
        self.assertEqual(st_2023.plots_seen, 1)
        self.assertEqual(st_2023.persons_seen, 1)
        st_2024 = import_file(csv_2024, fresh_db)
        self.assertEqual(st_2024.plots_seen, 1)
        self.assertEqual(st_2024.persons_seen, 1)
        self.assertEqual(st_2024.plots_updated, 1)

        r1 = search_plots(fresh_db, SearchQuery(global_id=2606338129))
        self.assertEqual(len(r1.results), 1)
        pl = r1.results[0]
        # PlaceNumber сохранён из старого, координаты и надпись обновлены из свежего
        self.assertEqual(pl.place_number, "ЗИ/002/2.3/2451")
        self.assertEqual(pl.lon, 37.646267)
        self.assertEqual(len(pl.persons), 1)
        self.assertIn("Белоцкая", pl.persons[0].raw_line)

        # Повторный импорт старого файла (2023) НЕ должен перезаписать свежую надпись 2024 года
        st_re = import_file(csv_2023, fresh_db)
        self.assertEqual(st_re.plots_updated, 0)
        r2 = search_plots(fresh_db, SearchQuery(global_id=2606338129))
        self.assertEqual(len(r2.results[0].persons), 1)
        self.assertIn("Белоцкая", r2.results[0].persons[0].raw_line)

        # 4. Очистка базы данных
        clear_db(fresh_db)
        meta_after_clear = get_db_meta(fresh_db)
        self.assertEqual(meta_after_clear["total_plots"], 0)
        self.assertEqual(meta_after_clear["total_persons"], 0)

        # 5. Защита от дубликатов при загрузке через браузер в папку input/
        in_dir = self.root / "input_test"
        tmp_dir = self.root / "tmp_test"
        payload = csv_2024.read_bytes()

        p_first, sha1, dup1 = save_uploaded_stream_to_input(
            io.BytesIO(payload), len(payload), "upload1.csv", in_dir, tmp_dir
        )
        self.assertFalse(dup1)
        self.assertTrue(p_first.is_file())

        p_second, sha2, dup2 = save_uploaded_stream_to_input(
            io.BytesIO(payload), len(payload), "upload1_copy.csv", in_dir, tmp_dir
        )
        self.assertTrue(dup2)
        self.assertEqual(sha1, sha2)
        self.assertEqual(p_first, p_second)
        self.assertEqual(len(list(in_dir.iterdir())), 1)

        # 6. Проверка работы БД, импорта, поиска и упаковки лога в длинном кириллическом пути с пробелами
        cyr_base = (
            self.root
            / "Папка пользователя с пробелами"
            / "Мои генеалогические архивы Москвы"
            / "Захоронения Москвы (портативная версия)"
        )
        cyr_db = cyr_base / "output" / "база захоронений москвы.db"
        cyr_input = cyr_base / "input"
        cyr_tmp = cyr_base / "tmp"
        cyr_log = cyr_base / "logs" / "app.log"
        cyr_input.mkdir(parents=True, exist_ok=True)
        cyr_csv = cyr_input / "Выгрузка реестра (19.03.2024).csv"
        cyr_csv.write_bytes(payload)

        st_cyr = import_file(cyr_csv, cyr_db)
        self.assertEqual(st_cyr.plots_seen, 1)
        r_cyr = search_plots(cyr_db, SearchQuery(person1=PersonFilter(fio="Белоцкая")))
        self.assertEqual(r_cyr.total_plots, 1)
        zip_cyr = save_and_reveal_diagnostic_zip(
            db_path=cyr_db,
            input_dir=cyr_input,
            tmp_dir=cyr_tmp,
            log_path=cyr_log,
            reveal=False,
        )
        self.assertTrue(zip_cyr.is_file())


def _build_minimal_xlsx_bytes(
    rows: list[list[str]],
    corrupt_xml: bool = False,
) -> bytes:
    """Создаёт в памяти минимальный ZIP-архив XLSX с заданными строками ячеек."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets>'
            "</workbook>",
        )
        row_xml_parts: list[str] = []
        for r_idx, row in enumerate(rows, start=1):
            cells_xml: list[str] = []
            for c_idx, val in enumerate(row):
                col_letter = chr(ord("A") + c_idx)
                esc_val = (
                    val.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                )
                cells_xml.append(
                    f'<c r="{col_letter}{r_idx}" t="inlineStr"><is><t>{esc_val}</t></is></c>'
                )
            row_xml_parts.append(f'<row r="{r_idx}">{"".join(cells_xml)}</row>')

        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>" + "".join(row_xml_parts)
        )
        if not corrupt_xml:
            sheet_xml += "</sheetData></worksheet>"
        else:
            sheet_xml += '<row r="999"><c r="A999" t="inlineStr"><is><t>Оборванный тег'
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml.encode("utf-8"))
    return buf.getvalue()


class LegacyDbRenameTest(unittest.TestCase):
    """Автоматическое переименование базы graves.db (версия 0.1.0) в burials.db."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp_dir.name) / "output"
        self.out.mkdir()
        self.new = self.out / "burials.db"
        self.legacy = self.out / "graves.db"

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _make_legacy_db_with_pending_wal(self) -> sqlite3.Connection:
        """Создаёт graves.db, у которой последняя запись лежит только в -wal (соединение открыто)."""
        conn = sqlite3.connect(str(self.legacy))
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES ('из wal')")
        conn.commit()
        self.assertTrue(Path(f"{self.legacy}-wal").stat().st_size > 0)
        return conn

    def test_renames_db_with_wal_and_keeps_data(self) -> None:
        """graves.db вместе с -wal переносится в burials.db, последняя запись не теряется."""
        conn = self._make_legacy_db_with_pending_wal()
        try:
            # Копируем файлы «как есть», пока соединение открыто: так -wal не будет слит при закрытии.
            snapshot = self.out / "snap"
            snapshot.mkdir()
            for suffix in ("", "-wal", "-shm"):
                shutil.copy(f"{self.legacy}{suffix}", snapshot / f"graves.db{suffix}")
        finally:
            conn.close()
        for f in self.out.glob("graves.db*"):
            f.unlink()
        for f in snapshot.iterdir():
            shutil.move(str(f), self.out / f.name)

        self.assertEqual(migrate_legacy_db_file(self.new), self.new)
        self.assertEqual(list(self.out.glob("graves.db*")), [])
        conn = sqlite3.connect(str(self.new))
        try:
            self.assertEqual(conn.execute("SELECT v FROM t").fetchall(), [("из wal",)])
        finally:
            conn.close()

    def test_noop_cases(self) -> None:
        """Ничего не трогает: нет старой базы, новая уже есть, путь не к burials.db."""
        self.assertEqual(migrate_legacy_db_file(self.new), self.new)

        self.legacy.write_bytes(b"old")
        self.new.write_bytes(b"new")
        self.assertEqual(migrate_legacy_db_file(self.new), self.new)
        self.assertEqual(self.legacy.read_bytes(), b"old")
        self.assertEqual(self.new.read_bytes(), b"new")

        custom = self.out / "custom.db"
        self.new.unlink()
        self.assertEqual(migrate_legacy_db_file(custom), custom)
        self.assertTrue(self.legacy.exists())

    def test_failed_rename_rolls_back_and_uses_legacy(self) -> None:
        """Если основной файл не переименовался, служебные файлы возвращаются, работа идёт со старым."""
        self.legacy.write_bytes(b"db")
        Path(f"{self.legacy}-wal").write_bytes(b"wal")
        real_rename = Path.rename

        def _flaky_rename(src: Path, dst: Path) -> Path:
            if src == self.legacy:
                raise PermissionError("файл занят")
            return real_rename(src, dst)

        with patch.object(Path, "rename", _flaky_rename):
            self.assertEqual(migrate_legacy_db_file(self.new), self.legacy)
        self.assertEqual(self.legacy.read_bytes(), b"db")
        self.assertEqual(Path(f"{self.legacy}-wal").read_bytes(), b"wal")
        self.assertEqual(list(self.out.glob("burials.db*")), [])


class ImportCancelTest(unittest.TestCase):
    """Остановка импорта на любом этапе полностью откатывает изменения в базе."""

    HEADER = (
        "PlaceNumber;GraveType;HasTombstone;Inscription;Cemetery;global_id\n"
        "Номер места;Тип;Наличие;Надпись;Кладбище;global_id\n"
    )

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.db_path = self.root / "cancel.db"
        base_csv = self.root / "base.csv"
        base_csv.write_text(
            self.HEADER
            + '"ЗИ/002/1.1/0001";"гробом в землю";"да";'
            '"Inscription:Красновский Евгений Яковлевич,год 1910,год 1980\n\n";"Алексеевское";"2606339001"\n',
            encoding="utf-8",
        )
        import_file(base_csv, self.db_path)

        self.new_csv = self.root / "new.csv"
        self.new_csv.write_text(
            self.HEADER
            + '"ЗИ/002/1.1/0001";"гробом в землю";"да";'
            '"Inscription:Красновский Евгений Яковлевич,год 1910,год 1981\n\n";"Алексеевское";"2606339001"\n'
            + '"ЗИ/002/1.1/0002";"гробом в землю";"да";'
            '"Inscription:Отменённый Пётр Ильич,год 1920,год 1990\n\n";"Алексеевское";"2606339002"\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _dump_db(self) -> list[str]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            return list(conn.iterdump())
        finally:
            conn.close()

    def test_cancel_during_reading_rolls_back(self) -> None:
        """Отмена на этапе чтения: база (включая FTS и import_history) не меняется."""
        before = self._dump_db()
        token = ImportCancelToken()

        def _progress(info: dict) -> None:
            if info.get("phase") == "reading":
                token.cancel()

        with self.assertRaises(ImportCancelledError):
            import_file(self.new_csv, self.db_path, progress_cb=_progress, cancel_token=token)
        self.assertEqual(self._dump_db(), before)

    def test_cancel_interrupts_long_index_rebuild_and_rolls_back(self) -> None:
        """Отмена прерывает выполняющийся SQL-запрос индексации и откатывает уже записанные данные."""
        before = self._dump_db()
        token = ImportCancelToken()
        started = threading.Event()

        def _endless_rebuild(conn: sqlite3.Connection, on_step=None) -> None:
            if on_step is not None:
                on_step(1, 2, "тест")
            started.set()
            # Бесконечный запрос: завершиться он может только через отмену (progress handler).
            conn.execute(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                "SELECT count(*) FROM c"
            ).fetchone()

        canceller = threading.Thread(target=lambda: started.wait(10) and token.cancel())
        canceller.start()
        with patch("src.importer.rebuild_fts_indexes", _endless_rebuild):
            with self.assertRaises(ImportCancelledError):
                import_file(self.new_csv, self.db_path, cancel_token=token)
        canceller.join(10)
        self.assertEqual(self._dump_db(), before)

        # После отмены тот же файл импортируется штатно.
        stats = import_file(self.new_csv, self.db_path)
        self.assertEqual(stats.total_plots_in_db, 2)
        found = search_plots(self.db_path, SearchQuery(person1=PersonFilter(fio="Отменённый")))
        self.assertEqual(found.total_plots, 1)

    def test_http_cancel_endpoint(self) -> None:
        """POST /api/import/cancel останавливает фоновый импорт; без импорта отвечает 409."""
        input_dir = self.root / "input"
        input_dir.mkdir()
        shutil.copy(self.new_csv, input_dir / "new.csv")
        before = self._dump_db()
        started = threading.Event()
        release = threading.Event()

        def _blocking_rebuild(conn: sqlite3.Connection, on_step=None) -> None:
            if on_step is not None:
                on_step(1, 2, "тест")
            started.set()
            release.wait(10)
            if on_step is not None:
                on_step(2, 2, "тест")

        server = create_http_server(
            db_path=self.db_path,
            port=0,
            input_dir=input_dir,
            tmp_dir=self.root / "tmp",
            log_path=self.root / "app.log",
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def _post(path: str, payload: dict | None = None) -> tuple[int, dict]:
            req = urllib.request.Request(
                f"{base}{path}",
                data=json.dumps(payload or {}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                with exc:
                    return exc.code, json.loads(exc.read())

        def _status() -> dict:
            with urllib.request.urlopen(f"{base}/api/import/status") as resp:
                return json.loads(resp.read())

        try:
            self.assertEqual(_post("/api/import/cancel")[0], 409)
            with patch("src.importer.rebuild_fts_indexes", _blocking_rebuild):
                code, _ = _post("/api/import/start", {"file_name": "new.csv"})
                self.assertEqual(code, 200)
                self.assertTrue(started.wait(10))
                self.assertEqual(_post("/api/import/cancel"), (200, {"status": "cancelling"}))
                self.assertTrue(_status()["cancel_requested"])
                release.set()
                deadline = time.monotonic() + 10
                st = _status()
                while st["running"] and time.monotonic() < deadline:
                    time.sleep(0.05)
                    st = _status()
            self.assertEqual(st["phase"], "cancelled")
            self.assertIsNone(st["error"])
        finally:
            release.set()
            server.shutdown()
            server.server_close()
        self.assertEqual(self._dump_db(), before)


class InputValidationTest(unittest.TestCase):
    """Полная проверка Фазы 1 и Фазы 2 валидации входных файлов, атомарности и защиты input/."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.db_path = self.root / "val_burials.db"

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_1_empty_and_whitespace_only_files(self) -> None:
        """П.1: Пустой файл (0 байт) и файлы только из пробелов/переносов строк."""
        missing_path = self.root / "no_such_file.csv"
        with self.assertRaises(DataValidationError) as ctx_miss:
            validate_source_file(missing_path)
        self.assertIn("не найден", str(ctx_miss.exception))

        for ext, content in (
            (".csv", b""),
            (".json", b"   \n\t\r\n  "),
            (".xml", b"\xef\xbb\xbf   \n  "),
            (".xlsx", b""),
        ):
            p = self.root / f"empty{ext}"
            p.write_bytes(content)
            with self.assertRaises(DataValidationError) as ctx:
                import_file(p, self.db_path)
            self.assertIn("пуст", str(ctx.exception).lower())

    def test_2_archives_by_magic_bytes_and_extension(self) -> None:
        """П.2: Архивы .zip (с датасетом внутри), .rar, .7z, .gz (включая переименованные в .xlsx/.csv)."""
        # Обычный .zip с вложенным data-64023.csv
        zip_path = self.root / "data-64023.csv.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("data-64023.csv", "global_id;Cemetery\n1;Алексеевское\n")
        with self.assertRaises(DataValidationError) as ctx_zip:
            import_file(zip_path, self.db_path)
        self.assertIn("нераспакованный zip-архив", str(ctx_zip.exception).lower())
        self.assertIn("data-64023.csv", str(ctx_zip.exception))

        # Тот же .zip, но переименованный пользователем в .xlsx
        disguised_zip = self.root / "disguised.xlsx"
        disguised_zip.write_bytes(zip_path.read_bytes())
        with self.assertRaises(DataValidationError) as ctx_dzip:
            import_file(disguised_zip, self.db_path)
        self.assertIn("data-64023.csv", str(ctx_dzip.exception))

        # .rar, .7z, .gz (как со своим расширением, так и переименованные в .xlsx / .csv)
        archive_cases = [
            ("dump.rar", b"Rar!\x1a\x07\x00\xcf\x90s\x00\x00\x0d", "rar"),
            ("renamed_rar.xlsx", b"Rar!\x1a\x07\x01\x00\x12\x34", "rar"),
            ("dump.7z", b"7z\xbc\xaf\x27\x1c\x00\x04\x00\x00", "7-zip"),
            ("renamed_7z.csv", b"7z\xbc\xaf\x27\x1c\x00\x04\x00\x00", "7-zip"),
            ("dump.gz", b"\x1f\x8b\x08\x00\x00\x00\x00\x00", "gzip"),
            ("renamed_gz.json", b"\x1f\x8b\x08\x00\x00\x00\x00\x00", "gzip"),
        ]
        for fname, magic_bytes, expected_token in archive_cases:
            p = self.root / fname
            p.write_bytes(magic_bytes)
            with self.assertRaises(DataValidationError) as ctx_arc:
                import_file(p, self.db_path)
            self.assertIn(expected_token, str(ctx_arc.exception).lower())

    def test_3_xls_pdf_and_html_disguised_as_datasets(self) -> None:
        """П.3: Старый Excel .xls (OLE2), .pdf и сохранённая веб-страница .html (в т.ч. под чужим расширением)."""
        cases = [
            (
                "old_excel.xls",
                b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
                "97–2003 (.xls)",
            ),
            (
                "disguised_ole2.xlsx",
                b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
                "97–2003 (.xls)",
            ),
            ("report.pdf", b"%PDF-1.7\n1 0 obj\n", "pdf"),
            ("disguised_pdf.csv", b"%PDF-1.5\n%test\n", "pdf"),
            (
                "portal_page.html",
                b"<!DOCTYPE html>\n<html><head><title>data.mos.ru</title></head><body></body></html>",
                "html",
            ),
            (
                "saved_webpage.xlsx",
                b"  \n<HTML lang='ru'><head></head><body>404 Not Found</body></html>",
                "html",
            ),
            (
                "saved_webpage.csv",
                b"<!doctype html><html><body>Error</body></html>",
                "html",
            ),
            (
                "saved_webpage.json",
                b"<html><head><meta charset='utf-8'></head><body>Portal</body></html>",
                "html",
            ),
            (
                "saved_webpage.xml",
                b"<!DOCTYPE HTML PUBLIC '-//W3C//DTD HTML 4.01//EN'><html><body></body></html>",
                "html",
            ),
        ]
        for fname, raw_bytes, expected_sub in cases:
            p = self.root / fname
            p.write_bytes(raw_bytes)
            with self.assertRaises(DataValidationError) as ctx:
                import_file(p, self.db_path)
            self.assertIn(expected_sub.lower(), str(ctx.exception).lower())

    def test_4_corrupted_and_truncated_files_and_atomic_rollback(self) -> None:
        """П.4: Оборванные .xlsx, .json, .xml, бинарный .csv с \\x00 и атомарный откат транзакции SQLite."""
        # 4.1. Оборванный ZIP-контейнер .xlsx
        bad_zip_xlsx = self.root / "truncated_zip.xlsx"
        bad_zip_xlsx.write_bytes(b"PK\x03\x04\x14\x00\x00\x00\x08\x00truncated_bytes")
        with self.assertRaises(DataValidationError) as ctx_bzip:
            import_file(bad_zip_xlsx, self.db_path)
        self.assertIn("повреждён", str(ctx_bzip.exception).lower())

        # 4.2. Валидный ZIP .xlsx, но битый XML внутри xl/worksheets/sheet1.xml
        corrupt_sheet_xlsx = self.root / "corrupt_sheet.xlsx"
        corrupt_sheet_xlsx.write_bytes(
            _build_minimal_xlsx_bytes(
                [
                    ["global_id", "Cemetery", "Inscription"],
                    ["2606331001", "Алексеевское", "Иванов Иван,год 1900,год 1970"],
                ],
                corrupt_xml=True,
            )
        )
        with self.assertRaises(DataValidationError) as ctx_xml_xlsx:
            import_file(corrupt_sheet_xlsx, self.db_path)
        self.assertIn("повреждён или обрезан", str(ctx_xml_xlsx.exception).lower())
        self.assertEqual(get_db_meta(self.db_path)["total_plots"], 0)

        # 4.3. Оборванный посередине JSON data.mos.ru (проверка атомарного отката!)
        # Сначала запишем больше BATCH_SIZE (2000) валидных участков, а затем оборвём JSON на середине строки
        lines = ["["]
        for idx in range(2100):
            gid = 2700000000 + idx
            lines.append(
                json.dumps(
                    {
                        "global_id": gid,
                        "Cemetery": "Алексеевское",
                        "GraveType": "гробом в землю",
                        "HasTombstone": "да",
                        "Inscription": [
                            {"Inscription": f"Тестовый Человек {idx},год 1910,год 1980"}
                        ],
                    },
                    ensure_ascii=False,
                )
                + ","
            )
        lines.append(
            '{"global_id": 2700009999, "Cemetery": "Ваганьковское", "Inscription": "Оборванная запись'
        )
        truncated_json = self.root / "truncated_dump.json"
        truncated_json.write_text("\n".join(lines), encoding="utf-8")

        with self.assertRaises(DataValidationError) as ctx_tjson:
            import_file(truncated_json, self.db_path)
        self.assertIn("обрезан", str(ctx_tjson.exception).lower())
        # Несмотря на то, что первые 2000 участков уже прошли через _flush_batch,
        # транзакция должна полностью откатиться (в БД 0 участков)!
        meta_after_rollback = get_db_meta(self.db_path)
        self.assertEqual(meta_after_rollback["total_plots"], 0)
        self.assertEqual(meta_after_rollback["total_persons"], 0)

        # 4.4. Оборванный .xml (незакрытый тег)
        truncated_xml = self.root / "truncated.xml"
        truncated_xml.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<root><item><global_id>2606331002</global_id><Cemetery>Алексеевское</Cemetery></item>"
            "<item><global_id>2606331003</global_id><Cemetery>Незакрытый",
            encoding="utf-8",
        )
        with self.assertRaises(DataValidationError) as ctx_txml:
            import_file(truncated_xml, self.db_path)
        self.assertIn("xml", str(ctx_txml.exception).lower())
        self.assertEqual(get_db_meta(self.db_path)["total_plots"], 0)

        # 4.5. CSV с нулевыми байтами \x00
        nul_csv = self.root / "binary_nulls.csv"
        nul_csv.write_bytes(
            b"global_id;Cemetery;Inscription\n2606331004;\xd0\x90\x00\x00\xd0\xb1;Test\n"
        )
        with self.assertRaises(DataValidationError) as ctx_nul:
            import_file(nul_csv, self.db_path)
        self.assertIn("нулевые байты", str(ctx_nul.exception).lower())

    def test_5_foreign_valid_files_and_empty_json_array(self) -> None:
        """П.5: Чужие валидные файлы (.xlsx, .csv, .json, .xml) без колонки/полей global_id и пустой []."""
        # Чужой .xlsx с колонками ID, Name, Price
        foreign_xlsx = self.root / "price_list.xlsx"
        foreign_xlsx.write_bytes(
            _build_minimal_xlsx_bytes(
                [
                    ["ID", "Name", "Price"],
                    ["1", "Товар 1", "100.00"],
                    ["2", "Товар 2", "200.00"],
                ]
            )
        )
        with self.assertRaises(DataValidationError) as ctx_fxlsx:
            import_file(foreign_xlsx, self.db_path)
        self.assertIn("global_id", str(ctx_fxlsx.exception))
        self.assertIn("ID, Name, Price", str(ctx_fxlsx.exception))

        # Чужой .csv с колонками id;name;value
        foreign_csv = self.root / "foreign.csv"
        foreign_csv.write_text(
            "id;name;value\n1;foo;bar\n2;baz;qux\n", encoding="utf-8"
        )
        with self.assertRaises(DataValidationError) as ctx_fcsv:
            import_file(foreign_csv, self.db_path)
        self.assertIn("global_id", str(ctx_fcsv.exception))
        self.assertIn("id, name, value", str(ctx_fcsv.exception))

        # Чужой .json (включая нестандартные производные форматы без global_id)
        for bad_json_body in (
            '[{"foo": "bar"}, {"baz": 123}]',
            '[{"id": "2606331479", "line": "Тест,год 1900,год 1970", "🗺️": "Алексеевское"}]',
        ):
            foreign_json = self.root / "foreign.json"
            foreign_json.write_text(bad_json_body, encoding="utf-8")
            with self.assertRaises(DataValidationError) as ctx_fjson:
                import_file(foreign_json, self.db_path)
            self.assertIn("неизвестную структуру", str(ctx_fjson.exception).lower())

        # Пустой JSON-массив []
        empty_arr_json = self.root / "empty_array.json"
        empty_arr_json.write_text("[\n]\n", encoding="utf-8")
        with self.assertRaises(DataValidationError) as ctx_ejson:
            import_file(empty_arr_json, self.db_path)
        self.assertIn("не найдено ни одной записи", str(ctx_ejson.exception).lower())

        # Чужой .xml
        foreign_xml = self.root / "foreign.xml"
        foreign_xml.write_text(
            '<?xml version="1.0" encoding="utf-8"?><root><item><name>Тест</name></item></root>',
            encoding="utf-8",
        )
        with self.assertRaises(DataValidationError) as ctx_fxml:
            import_file(foreign_xml, self.db_path)
        self.assertIn("не найдено записей", str(ctx_fxml.exception).lower())

    def test_6_zero_plots_protection_and_russian_headers_support(self) -> None:
        """П.6: Защита при plots_seen == 0, сохранность import_history и поддержка русских заголовков / UTF-16."""
        # Таблица с валидной шапкой global_id;Cemetery, но без строк данных (plots_seen == 0)
        header_only_csv = self.root / "header_only.csv"
        header_only_csv.write_text(
            "PlaceNumber;GraveType;HasTombstone;Inscription;Cemetery;global_id\n",
            encoding="utf-8",
        )
        with self.assertRaises(DataValidationError) as ctx_zero:
            import_file(header_only_csv, self.db_path)
        self.assertIn("не найдено ни одной записи", str(ctx_zero.exception).lower())

        # Проверим, что в import_history нет ни одной записи после всех ошибочных попыток
        sources = list_input_sources(self.root, self.db_path)
        self.assertEqual(len(sources["history"]), 0)

        # Проверим успешный импорт CSV в кодировке UTF-16 с русскими заголовками на 1-й строке
        ru_utf16_csv = self.root / "ru_headers_utf16.csv"
        ru_utf16_csv.write_bytes(
            (
                "Номер места (участка) захоронения;Тип захоронения на участке;"
                "Наличие надмогильного сооружения;Надпись на надмогильном сооружении;Кладбище;global_id\n"
                "ЗИ/002/3.3/1667;урной в землю;да;Щукина Людмила Александровна,16.03.1954,18.12.2015;"
                "Алексеевское;2606331479\n"
            ).encode("utf-16")
        )
        st = import_file(ru_utf16_csv, self.db_path)
        self.assertEqual(st.plots_seen, 1)
        self.assertEqual(st.persons_seen, 1)

        # Если после успешного импорта попытаться импортировать битый файл, существующие данные в БД не страдают
        bad_csv = self.root / "bad_after_good.csv"
        bad_csv.write_text("col1;col2\n1;2\n", encoding="utf-8")
        with self.assertRaises(DataValidationError):
            import_file(bad_csv, self.db_path)

        meta = get_db_meta(self.db_path)
        self.assertEqual(meta["total_plots"], 1)
        self.assertEqual(meta["total_persons"], 1)
        sources_after = list_input_sources(self.root, self.db_path)
        self.assertEqual(len(sources_after["history"]), 1)

    def test_7_upload_endpoint_rejects_bad_files_without_polluting_input(self) -> None:
        """П.7: POST /api/import/upload отклоняет битый/чужой файл, не сохраняет его в input/ и фиксирует ошибку."""
        in_dir = self.root / "web_input"
        tmp_dir = self.root / "web_tmp"
        srv = create_http_server(
            self.db_path,
            host="127.0.0.1",
            port=0,
            input_dir=in_dir,
            tmp_dir=tmp_dir,
        )
        host, port = srv.server_address[:2]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            base = f"http://{host}:{port}"
            bad_payload = b"id;name;price\n1;Apple;100\n"
            req = urllib.request.Request(
                f"{base}/api/import/upload?filename=wrong_table.csv",
                data=bad_payload,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(bad_payload)),
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx_http:
                urllib.request.urlopen(req)
            try:
                self.assertEqual(ctx_http.exception.code, 400)
                err_body = json.loads(ctx_http.exception.read().decode("utf-8"))
            finally:
                ctx_http.exception.close()
            self.assertIn("global_id", err_body["error"])

            # В папке input/ не должно остаться мусорного файла wrong_table.csv
            self.assertEqual(list(in_dir.iterdir()), [])

            # Эндпоинт /api/import/status также должен вернуть осмысленное описание ошибки
            st = json.loads(
                urllib.request.urlopen(f"{base}/api/import/status")
                .read()
                .decode("utf-8")
            )
            self.assertFalse(st["running"])
            self.assertEqual(st["phase"], "error")
            self.assertIn("global_id", st["error"])
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
