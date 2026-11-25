"""Структуры данных (dataclass) для импорта и поиска захоронений."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParsedPerson:
    """Разобранная надпись на памятнике (один человек или служебная запись).

    Attributes:
        raw_line: Исходная строка надписи без префикса Inscription:.
        surname: Первое слово ФИО в нижнем регистре (ё -> е) или None.
        firstname: Второе слово ФИО в нижнем регистре (ё -> е) или None.
        patronymic: Третье слово ФИО в нижнем регистре (ё -> е) или None.
        fio_norm: Полный нормализованный текст надписи без дат.
        birth_year: Год рождения (если распознаны две даты).
        birth_date_raw: Исходное представление даты рождения.
        death_year: Год смерти (если распознаны две даты).
        death_date_raw: Исходное представление даты смерти.
        single_date_year: Год единственной даты (если в надписи ровно одна дата).
        single_date_raw: Исходное представление единственной даты.
    """

    raw_line: str
    surname: str | None
    firstname: str | None
    patronymic: str | None
    fio_norm: str
    birth_year: int | None = None
    birth_date_raw: str | None = None
    death_year: int | None = None
    death_date_raw: str | None = None
    single_date_year: int | None = None
    single_date_raw: str | None = None


@dataclass
class PlotRecord:
    """Сырая запись участка захоронения из входного файла.

    Attributes:
        global_id: Уникальный идентификатор участка (data.mos.ru global_id).
        cemetery_name: Наименование кладбища.
        cemetery_code: Трёхзначный код кладбища из PlaceNumber (если есть).
        place_number: Полный шифр места захоронения (если есть).
        plot_section: Выделенный номер участка или колумбария (если есть).
        grave_type: Код типа захоронения (1 — гробом в землю, 2 — колумбарий, 3 — урной в землю).
        has_tombstone: Наличие надмогильного сооружения (1 — да, 0 — нет).
        inscriptions: Список сырых строк надписей на участке (без префикса Inscription:).
        lon: Долгота точки захоронения (если есть).
        lat: Широта точки захоронения (если есть).
        area: Площадь участка в кв. м (если есть).
    """

    global_id: int
    cemetery_name: str
    cemetery_code: str | None
    place_number: str | None
    plot_section: str | None
    grave_type: int
    has_tombstone: int
    inscriptions: list[str] = field(default_factory=list)
    lon: float | None = None
    lat: float | None = None
    area: float | None = None


@dataclass
class ImportStats:
    """Статистика выполнения импорта файла в базу данных.

    Attributes:
        file_path: Путь к импортированному файлу.
        plots_seen: Число прочитанных участков из файла.
        plots_inserted: Число новых добавленных участков.
        plots_updated: Число обновлённых существующих участков.
        persons_inserted: Число записанных строк надписей.
        total_plots_in_db: Итоговое число участков в БД после импорта.
        total_persons_in_db: Итоговое число надписей в БД после импорта.
        elapsed_sec: Затраченное время в секундах.
        persons_seen: Число дедуплицированных надписей захоронений, прочитанных из файла.
        source_date: Дата актуальности источника в формате 'YYYY-MM-DD' (если определена).
        sha256: SHA-256 хэш импортированного файла (если вычислен).
    """

    file_path: str
    plots_seen: int
    plots_inserted: int
    plots_updated: int
    persons_inserted: int
    total_plots_in_db: int
    total_persons_in_db: int
    elapsed_sec: float
    persons_seen: int = 0
    source_date: str | None = None
    sha256: str | None = None


@dataclass
class PersonFilter:
    """Фильтр поиска по человеку (ФИО1 или ФИО2).

    Attributes:
        fio: Строка запроса ФИО (поддерживает слова, кавычки "..." для точной формы,
            точку '.' для инициалов, '$' для конца слова, '|' для альтернатив,
            '*' и '?' для масок, '~' для 0 или 1 любого символа).
        search_mode: Режим поиска ('prefix_default', 'prefix_no_surname', 'substring').
        birth_exact: Точный год ('1954'), месяц и год ('03.1954'), дата рождения ('16.03.1954')
            или диапазон дат через дефис ('1900-16.03.1954', '1900-', '-1954').
        birth_from: Год рождения «с» (включительно).
        birth_to: Год рождения «по» (включительно).
        death_exact: Точный год ('2015'), месяц и год ('12.2015'), дата смерти ('18.12.2015')
            или диапазон дат через дефис ('1970-18.12.2015', '1970-', '-2015').
        death_from: Год смерти «с» (включительно).
        death_to: Год смерти «по» (включительно).
    """

    fio: str = ""
    search_mode: str = "prefix_default"
    birth_exact: str | None = None
    birth_from: int | None = None
    birth_to: int | None = None
    death_exact: str | None = None
    death_from: int | None = None
    death_to: int | None = None

    def is_active(self) -> bool:
        """Возвращает True, если задано хотя бы одно условие фильтрации человека."""
        return bool(
            (self.fio and self.fio.strip())
            or (self.birth_exact and str(self.birth_exact).strip())
            or self.birth_from is not None
            or self.birth_to is not None
            or (self.death_exact and str(self.death_exact).strip())
            or self.death_from is not None
            or self.death_to is not None
        )


@dataclass
class SearchQuery:
    """Полный типизированный поисковый запрос.

    Attributes:
        person1: Фильтр первого человека (ФИО1 + даты).
        person2: Фильтр второго человека на том же участке (ФИО2 + даты).
        cemetery_id: Фильтр по числовому ID кладбища в таблице cemeteries.
        cemetery_name: Фильтр по названию кладбища.
        place_number: Фильтр по номеру участка (plot_section или place_number).
        global_id: Точный поиск по global_id участка.
        limit: Максимальное число возвращаемых участков.
        offset: Смещение для пагинации.
    """

    person1: PersonFilter = field(default_factory=PersonFilter)
    person2: PersonFilter | None = None
    cemetery_id: int | None = None
    cemetery_name: str | None = None
    place_number: str | None = None
    global_id: int | None = None
    limit: int = 50
    offset: int = 0


@dataclass
class PersonResult:
    """Надпись на найденном участке в результатах поиска.

    Attributes:
        id: Внутренний идентификатор записи в таблице persons.
        raw_line: Исходная строка надписи.
        fio_norm: Нормализованный текст надписи без дат.
        birth_date_raw: Сырая строка даты рождения (если есть).
        death_date_raw: Сырая строка даты смерти (если есть).
        single_date_raw: Сырая строка единственной даты (если есть).
        birth_year: Год рождения (если есть).
        death_year: Год смерти (если есть).
        single_date_year: Год единственной даты (если есть).
        matched_p1: Совпала ли запись с условиями фильтра ФИО1.
        matched_p2: Совпала ли запись с условиями фильтра ФИО2.
        fio_display: Очищенное ФИО для отображения (без токенов дат).
        dates_display: Отформатированная строка дат жизни для отображения.
    """

    id: int
    raw_line: str
    fio_norm: str
    birth_date_raw: str | None
    death_date_raw: str | None
    single_date_raw: str | None
    birth_year: int | None
    death_year: int | None
    single_date_year: int | None
    matched_p1: bool = False
    matched_p2: bool = False
    fio_display: str = ""
    dates_display: str = ""


@dataclass
class PlotResult:
    """Найденный участок захоронения со всеми надписями на нём.

    Attributes:
        global_id: Идентификатор участка (global_id).
        cemetery_id: Идентификатор кладбища.
        cemetery_name: Название кладбища.
        place_number: Полный шифр участка (если известен).
        plot_section: Номер участка или колумбария (если известен).
        grave_type: Код типа захоронения (1, 2 или 3).
        grave_type_label: Человекочитаемое описание типа захоронения.
        has_tombstone: Наличие памятника (1 или 0).
        person_count: Общее число надписей на участке.
        lon: Долгота (если известна).
        lat: Широта (если известна).
        area: Площадь участка в кв. м (если известна).
        ya_url: Ссылка на Яндекс.Карты (если есть координаты).
        google_url: Ссылка на Google Maps (если есть координаты).
        osm_url: Ссылка на OpenStreetMap (если есть координаты).
        nakarte_url: Ссылка на nakarte.me со слоем Bing Satellite (если есть координаты).
        epoisk_map_url: Ссылка на схему кладбища epoisk.ru с выделением участка.
        persons: Полный список надписей на этом участке.
    """

    global_id: int
    cemetery_id: int
    cemetery_name: str
    place_number: str | None
    plot_section: str | None
    grave_type: int
    grave_type_label: str
    has_tombstone: int
    person_count: int
    lon: float | None
    lat: float | None
    area: float | None
    ya_url: str | None
    google_url: str | None = None
    osm_url: str | None = None
    nakarte_url: str | None = None
    epoisk_map_url: str | None = None
    persons: list[PersonResult] = field(default_factory=list)


@dataclass
class SearchResponse:
    """Ответ поискового модуля.

    Attributes:
        results: Список найденных участков на текущей странице.
        total_plots: Общее число участков, удовлетворяющих запросу (или оценка/точное число).
        has_more: Есть ли ещё результаты за пределами limit + offset.
        elapsed_ms: Время выполнения запроса в миллисекундах.
    """

    results: list[PlotResult]
    total_plots: int
    has_more: bool
    elapsed_ms: float
