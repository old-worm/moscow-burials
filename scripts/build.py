"""Скрипт сборки автономного дистрибутива приложения через PyInstaller (--onedir / .app)."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import struct
import subprocess
import sys
from typing import NamedTuple

from src.db import DB_FILE_NAME, DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

MACOS_BUNDLE_ID = "ru.mos.burials"
APP_DISPLAY_NAME = "Захоронения Москвы"
MACOS_DISPLAY_NAME = APP_DISPLAY_NAME
MACOS_MIN_VERSION = "10.15.0"
MACOS_DEPLOYMENT_TARGET = "10.15"
MACOS_ARM64_MIN_VERSION = "11.0"
MACOS_TARGET_ARCHES = ("x86_64", "arm64", "universal2")
MACOS_DEFAULT_TARGET_ARCH = "x86_64"
MACHO_SUFFIXES = (".so", ".dylib")
JUNK_FILE_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
# Окно установочного .dmg: размер в pt совпадает с холстом assets/dmg-background.svg,
# центры значков — с метками в нём же.
DMG_BACKGROUND_SVG_RELPATH = Path("assets") / "dmg-background.svg"
DMG_WINDOW_SIZE = (640, 400)
# window_rect в dmgbuild — рамка окна целиком; Finder вычитает из неё заголовок:
# 28 pt до macOS 15, 32 pt в macOS 26 (там низ фона обрезается на 4 pt — он пустой).
DMG_TITLE_BAR_PT = 28
DMG_ICON_SIZE = 128
DMG_TEXT_SIZE = 13
DMG_APP_ICON_POS = (160, 190)
DMG_APPLICATIONS_ICON_POS = (480, 190)
# Finder показывает имя симлинка как есть; интерфейс программы русский.
DMG_APPLICATIONS_LINK_NAME = "Программы"
# Суффикс архитектуры в имени macOS-релиза (по обычаю: universal, arm64, x86_64).
DMG_ARCH_SUFFIXES = {"universal2": "universal", "arm64": "arm64", "x86_64": "x86_64"}


class IconLayout(NamedTuple):
    """Раскладка иконки на квадратном холсте ICON_CANVAS_PX x ICON_CANVAS_PX.

    Attributes:
        tile_px: Сторона плитки-скруглённого квадрата по центру холста.
        corner_ratio: Радиус скругления углов плитки как доля её стороны.
        artwork_px: Высота всего щита (по viewBox SVG); фигура занимает ~85% от неё.
        shift_y_px: Сдвиг рисунка вниз: фигура в щите стоит выше его центра.
    """

    tile_px: int
    corner_ratio: float
    artwork_px: int
    shift_y_px: int


# Иконка — плитка цвета поля щита, на ней крупно всадник и дракон; контур щита убран.
ICON_CANVAS_PX = 1024
ICON_TILE_COLOR = "#A8002C"  # цвет поля щита в coat_of_arms.svg (группа g12)
# Группы coat_of_arms.svg, которые убираются с иконки: g8 — чёрный контур щита,
# g12 — красное поле (его заменяет плитка). Внутри них нет вложенных <g>.
ICON_HIDDEN_SVG_IDS = ("g8", "g12")
# macOS (Big Sur и новее): плитка 824 из 1024 px. macOS 26 Tahoe показывает иконку
# иной формы уменьшенной на серой подложке.
MACOS_ICON_LAYOUT = IconLayout(tile_px=824, corner_ratio=0.225, artwork_px=840, shift_y_px=27)
# Windows сама форму не навязывает: плитка почти во весь холст (поле 8 px на 256)
# со скруглением поменьше, иначе иконка в Проводнике мельче соседних.
WINDOWS_ICON_LAYOUT = IconLayout(tile_px=960, corner_ratio=0.16, artwork_px=979, shift_y_px=31)
WINDOWS_ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
WINDOWS_ICO_PNG_DIR = "AppIcon-windows"
# Иконка Windows хранится в репозитории: рендерить SVG умеет только сборка на macOS,
# она же обновляет этот файл, а сборка на Windows берёт его готовым.
WINDOWS_ICO_RELPATH = Path("assets") / "AppIcon.ico"


def _pack_pngs_to_ico(
    png_entries: list[tuple[int, Path]], ico_path: Path
) -> Path | None:
    """Упаковывает набор PNG-файлов разных размеров в стандартный файл иконки Windows (.ico).

    Args:
        png_entries: Список кортежей (размер_в_пикселях, путь_к_png).
        ico_path: Путь к создаваемому файлу .ico.

    Returns:
        Путь к созданному файлу .ico или None, если нет доступных PNG-файлов.
    """
    valid_payloads: list[tuple[int, bytes]] = []
    for size, png_path in png_entries:
        if png_path.is_file():
            data = png_path.read_bytes()
            if data:
                valid_payloads.append((size, data))

    if not valid_payloads:
        return None

    count = len(valid_payloads)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    dir_entries: list[bytes] = []
    image_blobs: list[bytes] = []

    for size, png_bytes in valid_payloads:
        dim = 0 if size >= 256 else size
        entry = struct.pack(
            "<BBBBHHII",
            dim,
            dim,
            0,
            0,
            1,
            32,
            len(png_bytes),
            offset,
        )
        dir_entries.append(entry)
        image_blobs.append(png_bytes)
        offset += len(png_bytes)

    ico_path.parent.mkdir(parents=True, exist_ok=True)
    ico_path.write_bytes(header + b"".join(dir_entries) + b"".join(image_blobs))
    return ico_path


def _make_square_icon_svg(svg_path: Path, out_path: Path, layout: IconLayout) -> Path:
    """Создаёт квадратную иконку: плитка цвета поля щита с фигурой герба.

    Контур и поле щита убираются, плитка-скруглённый квадрат заливается цветом
    поля, а рисунок центрируется и масштабируется так, чтобы фигура заполнила
    плитку. Для macOS плитка повторяет системную форму, чтобы macOS 26 Tahoe
    не сажала иконку на серую подложку.

    Args:
        svg_path: Исходный SVG с атрибутом viewBox у корневого элемента.
        out_path: Путь к создаваемому квадратному SVG.
        layout: Размеры плитки и рисунка на холсте.

    Returns:
        Путь к созданному SVG.

    Raises:
        ValueError: Если у корневого элемента нет корректного viewBox.
    """
    text = svg_path.read_text(encoding="utf-8")
    root_match = re.search(r"<svg\b[^>]*>", text)
    viewbox_match = root_match and re.search(r'\sviewBox="([^"]+)"', root_match.group(0))
    if not viewbox_match:
        raise ValueError(f"У корневого <svg> нет viewBox: {svg_path}")
    x, y, width, height = (
        float(part) for part in viewbox_match.group(1).replace(",", " ").split()
    )

    side = max(width, height) * ICON_CANVAS_PX / layout.artwork_px
    new_x = x - (side - width) / 2
    px = side / ICON_CANVAS_PX  # единиц viewBox на пиксель холста
    new_y = y - (side - height) / 2 - layout.shift_y_px * px
    tile_offset = (ICON_CANVAS_PX - layout.tile_px) / 2 * px
    tile_side = layout.tile_px * px
    tile_radius = tile_side * layout.corner_ratio
    tile = (
        f'<rect x="{new_x + tile_offset:.4f}" y="{new_y + tile_offset:.4f}" '
        f'width="{tile_side:.4f}" height="{tile_side:.4f}" '
        f'rx="{tile_radius:.4f}" ry="{tile_radius:.4f}" fill="{ICON_TILE_COLOR}"/>'
    )
    # Плитка задаёт форму иконки, поэтому всё, что выходит за неё, обрезается.
    clip = (
        '<clipPath id="appIconTileClip">'
        f'<rect x="{new_x + tile_offset:.4f}" y="{new_y + tile_offset:.4f}" '
        f'width="{tile_side:.4f}" height="{tile_side:.4f}" '
        f'rx="{tile_radius:.4f}" ry="{tile_radius:.4f}"/></clipPath>'
    )

    # Группы удаляются, а не скрываются: рендер SVG в macOS (sips) игнорирует display="none".
    for element_id in ICON_HIDDEN_SVG_IDS:
        text, found = re.subn(
            rf'<g\b[^>]*\sid="{element_id}"[^>]*>.*?</g>', "", text, count=1, flags=re.DOTALL
        )
        if not found:
            logger.warning("В %s нет группы id=%s, она останется на иконке.", svg_path, element_id)
    root_match = re.search(r"<svg\b[^>]*>", text)
    end_match = re.search(r"</svg>\s*$", text)
    if not root_match or not end_match:
        raise ValueError(f"Не удалось разобрать корневой <svg>: {svg_path}")

    root_tag = root_match.group(0)
    root_tag = re.sub(
        r'\sviewBox="[^"]*"',
        f' viewBox="{new_x:.4f} {new_y:.4f} {side:.4f} {side:.4f}"',
        root_tag,
    )
    root_tag = re.sub(r'\swidth="[^"]*"', f' width="{ICON_CANVAS_PX}"', root_tag)
    root_tag = re.sub(r'\sheight="[^"]*"', f' height="{ICON_CANVAS_PX}"', root_tag)

    out_path.write_text(
        text[: root_match.start()]
        + root_tag
        + f"<defs>{clip}</defs>{tile}"
        + '<g clip-path="url(#appIconTileClip)">'
        + text[root_match.end() : end_match.start()]
        + "</g></svg>\n",
        encoding="utf-8",
    )
    return out_path


def _render_svg_to_png(svg_path: Path, out_png: Path, tmp_dir: Path) -> bool:
    """Рендерит квадратный SVG в PNG 1024x1024 с прозрачным фоном.

    Сначала пробует sips (сохраняет прозрачность), затем qlmanage
    (подкладывает белый фон, но рисунок не обрезает).

    Args:
        svg_path: Квадратный SVG-файл.
        out_png: Путь к создаваемому PNG.
        tmp_dir: Рабочая директория для qlmanage.

    Returns:
        True, если PNG создан.
    """
    if out_png.exists():
        out_png.unlink()
    try:
        subprocess.run(
            ["sips", "-s", "format", "png", str(svg_path), "--out", str(out_png)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.info("sips не отрендерил SVG (%s), пробую qlmanage.", exc)
    if out_png.is_file():
        return True

    ql_png = tmp_dir / f"{svg_path.name}.png"
    if ql_png.exists():
        ql_png.unlink()
    subprocess.run(
        [
            "qlmanage",
            "-t",
            "-s",
            str(ICON_CANVAS_PX),
            "-o",
            str(tmp_dir),
            str(svg_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    if ql_png.is_file():
        ql_png.replace(out_png)
        logger.warning("Иконка отрендерена qlmanage: поля будут белыми, а не прозрачными.")
        return True
    return False


def _resize_png(src_png: Path, size: int, out_png: Path) -> None:
    """Масштабирует квадратный PNG до size x size утилитой sips.

    Args:
        src_png: Исходный PNG.
        size: Сторона результата в пикселях.
        out_png: Путь к создаваемому PNG.
    """
    subprocess.run(
        ["sips", "-z", str(size), str(size), str(src_png), "--out", str(out_png)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )


def _generate_windows_ico(svg_path: Path, tmp_dir: Path, ico_path: Path) -> Path | None:
    """Генерирует иконку Windows (.ico) с раскладкой для Windows встроенными утилитами macOS.

    Args:
        svg_path: Путь к исходному SVG-файлу (src/static/coat_of_arms.svg).
        tmp_dir: Временная рабочая директория (tmp/).
        ico_path: Путь к создаваемому .ico (assets/AppIcon.ico в репозитории).

    Returns:
        Путь к .ico или None, если отрендерить не удалось.
    """
    png_dir = tmp_dir / WINDOWS_ICO_PNG_DIR
    if png_dir.exists():
        shutil.rmtree(png_dir)
    png_dir.mkdir(parents=True)

    square_svg = _make_square_icon_svg(
        svg_path, tmp_dir / "AppIcon-windows.svg", WINDOWS_ICON_LAYOUT
    )
    base_png = tmp_dir / "AppIcon-windows.png"
    if not _render_svg_to_png(square_svg, base_png, tmp_dir):
        logger.warning("Не удалось отрендерить иконку Windows из %s.", svg_path)
        return None
    entries = []
    for size in WINDOWS_ICO_SIZES:
        png_path = png_dir / f"icon_{size}.png"
        _resize_png(base_png, size, png_path)
        entries.append((size, png_path))
    return _pack_pngs_to_ico(entries, ico_path)


def _generate_macos_icns(
    svg_path: Path, tmp_dir: Path, windows_ico_path: Path
) -> Path | None:
    """Генерирует иконки AppIcon.icns и AppIcon.ico из SVG встроенными утилитами macOS.

    Args:
        svg_path: Путь к исходному SVG-файлу (src/static/coat_of_arms.svg).
        tmp_dir: Временная рабочая директория (tmp/).
        windows_ico_path: Куда положить иконку Windows (assets/AppIcon.ico).

    Returns:
        Путь к созданному файлу AppIcon.icns или None, если генерация не удалась.
    """
    if not svg_path.is_file():
        logger.warning("Исходный файл иконки не найден: %s", svg_path)
        return None

    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        icns_path = tmp_dir / "AppIcon.icns"
        iconset_dir = tmp_dir / "AppIcon.iconset"
        if iconset_dir.exists():
            shutil.rmtree(iconset_dir)
        iconset_dir.mkdir(parents=True, exist_ok=True)

        square_svg = _make_square_icon_svg(
            svg_path, tmp_dir / "AppIcon.svg", MACOS_ICON_LAYOUT
        )
        base_png = tmp_dir / "AppIcon.png"
        if not _render_svg_to_png(square_svg, base_png, tmp_dir):
            logger.warning(
                "Не удалось отрендерить PNG из %s, сборка продолжится со стандартной иконкой.",
                svg_path,
            )
            return None

        icon_specs: list[tuple[int, str]] = [
            (16, "icon_16x16.png"),
            (32, "icon_16x16@2x.png"),
            (32, "icon_32x32.png"),
            (64, "icon_32x32@2x.png"),
            (128, "icon_128x128.png"),
            (256, "icon_128x128@2x.png"),
            (256, "icon_256x256.png"),
            (512, "icon_256x256@2x.png"),
            (512, "icon_512x512.png"),
            (1024, "icon_512x512@2x.png"),
        ]
        for size, filename in icon_specs:
            _resize_png(base_png, size, iconset_dir / filename)

        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        _generate_windows_ico(svg_path, tmp_dir, windows_ico_path)
        if icns_path.is_file():
            return icns_path
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        logger.warning(
            "Не удалось сгенерировать AppIcon.icns (%s), сборка продолжится без иконки.",
            exc,
        )
    return None


def _patch_and_sign_macos_app(app_bundle: Path) -> None:
    """Обновляет Info.plist в собранном .app бандле и переподписывает его ad-hoc подписью.

    Args:
        app_bundle: Путь к собранному бандлу dist/<name>.app.
    """
    info_plist = app_bundle / "Contents" / "Info.plist"
    if not info_plist.is_file():
        logger.warning("Файл Info.plist не найден: %s", info_plist)
        return

    with info_plist.open("rb") as fp:
        plist_data = plistlib.load(fp)

    plist_data["LSMinimumSystemVersion"] = MACOS_MIN_VERSION
    plist_data["CFBundleDisplayName"] = MACOS_DISPLAY_NAME
    plist_data["CFBundleName"] = MACOS_DISPLAY_NAME
    plist_data["NSHighResolutionCapable"] = True

    with info_plist.open("wb") as fp:
        plistlib.dump(plist_data, fp)

    logger.info("Обновлён Info.plist в %s", info_plist)

    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(app_bundle)],
        check=True,
    )
    logger.info("Выполнена ad-hoc подпись бандла: %s", app_bundle)


def _version_tuple(version: str) -> tuple[int, ...]:
    """Преобразует строку версии вида "10.13.0" в кортеж чисел для сравнения.

    Args:
        version: Строка версии из чисел, разделённых точками.

    Returns:
        Кортеж чисел без незначащих завершающих нулей, например (10, 13).
    """
    parts = [int(part) for part in version.split(".")]
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _required_arches(target_arch: str) -> set[str]:
    """Возвращает набор архитектур, которые обязаны быть в каждом Mach-O файле бандла.

    Args:
        target_arch: Целевая архитектура сборки (x86_64, arm64 или universal2).

    Returns:
        Набор имён архитектур в формате lipo.
    """
    if target_arch == "universal2":
        return {"x86_64", "arm64"}
    return {target_arch}


def _arch_max_min_version(arch: str) -> str:
    """Возвращает максимально допустимый minos для среза указанной архитектуры.

    Для x86_64 это MACOS_MIN_VERSION (LSMinimumSystemVersion). Mac на Apple Silicon
    не бывает с macOS старше 11.0, поэтому для arm64 порог не ниже MACOS_ARM64_MIN_VERSION.

    Args:
        arch: Архитектура среза в формате lipo (x86_64, arm64).

    Returns:
        Строка версии macOS.
    """
    if arch == "arm64":
        return max(MACOS_MIN_VERSION, MACOS_ARM64_MIN_VERSION, key=_version_tuple)
    return MACOS_MIN_VERSION


def _is_macho(path: Path) -> bool:
    """Проверяет по сигнатуре, является ли файл Mach-O бинарником (в т.ч. fat/universal).

    Args:
        path: Путь к проверяемому файлу.

    Returns:
        True, если файл начинается с одной из магических сигнатур Mach-O.
    """
    magics = {
        b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64 (little-endian)
        b"\xce\xfa\xed\xfe",  # MH_MAGIC (little-endian)
        b"\xca\xfe\xba\xbe",  # FAT_MAGIC
        b"\xca\xfe\xba\xbf",  # FAT_MAGIC_64
    }
    with path.open("rb") as fp:
        return fp.read(4) in magics


def _macho_min_versions(path: Path, arch: str) -> list[str]:
    """Возвращает минимальные версии macOS (minos) для одного архитектурного среза Mach-O.

    Использует `vtool -arch <arch> -show-build`; для старых бинарников без
    LC_BUILD_VERSION берётся `version` из LC_VERSION_MIN_MACOSX.

    Args:
        path: Путь к Mach-O файлу.
        arch: Архитектура среза в формате lipo (x86_64, arm64).

    Returns:
        Список строк версий (по одной на каждый найденный load command).
    """
    output = subprocess.run(
        ["vtool", "-arch", arch, "-show-build", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    versions: list[str] = []
    in_version_min = False
    for line in output.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "cmd":
            in_version_min = len(tokens) > 1 and tokens[1] == "LC_VERSION_MIN_MACOSX"
        elif tokens[0] == "minos" and len(tokens) > 1:
            versions.append(tokens[1])
        elif in_version_min and tokens[0] == "version" and len(tokens) > 1:
            versions.append(tokens[1])
    return versions


def _verify_macos_bundle(app_bundle: Path, target_arch: str) -> list[str]:
    """Проверяет совместимость всех Mach-O файлов бандла с заявленными архитектурой и macOS.

    Каждый исполняемый файл, .so и .dylib должен содержать все целевые архитектуры,
    а minos каждого целевого среза не должен превышать допустимый для него порог
    (см. _arch_max_min_version).

    Args:
        app_bundle: Путь к собранному бандлу dist/<name>.app.
        target_arch: Целевая архитектура сборки.

    Returns:
        Список описаний найденных проблем (пустой, если бандл совместим).
    """
    required = _required_arches(target_arch)
    problems: list[str] = []

    for path in sorted(app_bundle.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        is_candidate = path.suffix in MACHO_SUFFIXES or os.access(path, os.X_OK)
        if not is_candidate or not _is_macho(path):
            continue

        relative = path.relative_to(app_bundle)
        arches = set(
            subprocess.run(
                ["lipo", "-archs", str(path)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split()
        )
        missing = required - arches
        if missing:
            problems.append(
                f"{relative}: нет архитектур {', '.join(sorted(missing))} "
                f"(есть: {', '.join(sorted(arches)) or '—'})"
            )

        for arch in sorted(arches & required):
            allowed = _arch_max_min_version(arch)
            too_new = sorted(
                {
                    version
                    for version in _macho_min_versions(path, arch)
                    if _version_tuple(version) > _version_tuple(allowed)
                }
            )
            if too_new:
                problems.append(
                    f"{relative} [{arch}]: требует macOS {', '.join(too_new)} "
                    f"(допустимо не выше {allowed})"
                )
    return problems


def _remove_junk_files(target: Path) -> None:
    """Удаляет служебные файлы ОС (.DS_Store, ._*, Thumbs.db, desktop.ini) внутри target.

    Args:
        target: Папка собранного приложения (dist/<name>.app или dist/<name>).
    """
    for junk in target.rglob("*"):
        if junk.is_file() and (
            junk.name in JUNK_FILE_NAMES or junk.name.startswith("._")
        ):
            junk.unlink()
            logger.info("Удалён служебный файл: %s", junk)


def _render_dmg_background(svg_path: Path, out_dir: Path) -> Path:
    """Рендерит фон окна .dmg из SVG в PNG 1x и @2x (Retina) утилитой sips.

    dmgbuild сам находит рядом файл <имя>@2x.png и склеивает оба в один
    многоразмерный TIFF, который Finder показывает чётко на любом экране.

    Args:
        svg_path: Макет фона (assets/dmg-background.svg) размером DMG_WINDOW_SIZE pt.
        out_dir: Папка для PNG.

    Returns:
        Путь к PNG 1x (рядом лежит @2x).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_text = svg_path.read_text(encoding="utf-8")
    width, height = DMG_WINDOW_SIZE
    size_attrs = f'width="{width}" height="{height}"'
    if size_attrs not in svg_text:
        raise ValueError(f"{svg_path}: корневой <svg> должен иметь {size_attrs}")

    base_png = out_dir / "background.png"
    for scale, png in ((1, base_png), (2, out_dir / "background@2x.png")):
        scaled_svg = out_dir / f"background@{scale}x.svg"
        scaled_svg.write_text(
            svg_text.replace(
                size_attrs, f'width="{width * scale}" height="{height * scale}"', 1
            ),
            encoding="utf-8",
        )
        subprocess.run(
            ["sips", "-s", "format", "png", str(scaled_svg), "--out", str(png)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    return base_png


def _verify_dmg(dmg_path: Path, app_name: str, work_dir: Path) -> None:
    """Проверяет контрольную сумму образа и подпись приложения внутри него.

    Args:
        dmg_path: Готовый образ.
        app_name: Имя бандла в корне тома (например, «Захоронения Москвы.app»).
        work_dir: Рабочая папка для точки монтирования.

    Raises:
        subprocess.CalledProcessError: Образ повреждён или подпись приложения нарушена.
    """
    subprocess.run(["hdiutil", "verify", "-quiet", str(dmg_path)], check=True)
    mount_dir = work_dir / "verify-mnt"
    mount_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["hdiutil", "attach", "-quiet", "-readonly", "-nobrowse", "-noautoopen",
         "-mountpoint", str(mount_dir), str(dmg_path)],
        check=True,
    )
    try:
        subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(mount_dir / app_name)],
            check=True,
        )
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", str(mount_dir)], check=True)


def _make_release_dmg(
    app_bundle: Path, dmg_path: Path, icns_path: Path | None, background_svg: Path
) -> Path:
    """Упаковывает .app в оформленный сжатый образ диска .dmg для публикации в Releases.

    Окно образа: светлый фон со стрелкой (Finder пишет подписи значков чёрным даже
    в тёмной теме), приложение слева, ярлык «Программы» на /Applications справа,
    без панелей Finder; иконка тома — иконка приложения. Расширение .app не скрываем
    флагом: он пишет com.apple.FinderInfo на бандл и ломает строгую проверку подписи
    (Finder и так скрывает .app, если не включён показ всех расширений).
    HFS+ и сжатие ULMO (lzma) поддерживаются начиная с macOS 10.15 — нашего минимума.
    .DS_Store окна пишет dmgbuild сам, без AppleScript и Finder, поэтому сборка
    работает и без графического сеанса.

    Args:
        app_bundle: Путь к собранному и подписанному бандлу dist/<name>.app.
        dmg_path: Путь к создаваемому образу (ASCII-имя, см. _release_stem).
        icns_path: Иконка тома (AppIcon.icns) или None.
        background_svg: Макет фона окна (assets/dmg-background.svg).

    Returns:
        Путь к созданному образу.
    """
    from dmgbuild import build_dmg

    _remove_junk_files(app_bundle)

    work_dir = app_bundle.parent.parent / "tmp" / "dmg"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)
    if dmg_path.exists():
        dmg_path.unlink()

    # Маркер no_log запрещает fseventsd писать журнал в образ при монтировании.
    fseventsd_dir = work_dir / ".fseventsd"
    fseventsd_dir.mkdir()
    (fseventsd_dir / "no_log").touch()

    background_png = _render_dmg_background(background_svg, work_dir / "background")
    window_w, window_h = DMG_WINDOW_SIZE
    settings = {
        "format": "ULMO",
        "filesystem": "HFS+",
        "files": [str(app_bundle), str(fseventsd_dir)],
        "symlinks": {DMG_APPLICATIONS_LINK_NAME: "/Applications"},
        "icon": str(icns_path) if icns_path is not None and icns_path.is_file() else None,
        "background": str(background_png),
        "window_rect": ((200, 120), (window_w, window_h + DMG_TITLE_BAR_PT)),
        "default_view": "icon-view",
        "icon_size": DMG_ICON_SIZE,
        "text_size": DMG_TEXT_SIZE,
        "icon_locations": {
            app_bundle.name: DMG_APP_ICON_POS,
            DMG_APPLICATIONS_LINK_NAME: DMG_APPLICATIONS_ICON_POS,
        },
    }
    build_dmg(str(dmg_path), MACOS_DISPLAY_NAME, settings=settings)
    _verify_dmg(dmg_path, app_bundle.name, work_dir)
    shutil.rmtree(work_dir)
    logger.info("Создан образ диска: %s", dmg_path)
    return dmg_path


def _release_stem(project_root: Path, name: str, os_tag: str, arch_tag: str) -> str:
    """Собирает имя релизного файла (без расширения) по обычаю «имя-версия-ОС-архитектура».

    Только ASCII: GitHub Releases заменяет пробелы и не-ASCII символы точками.
    Версия читается текстом из src/__init__.py (__version__), без импорта пакета.

    Args:
        project_root: Корень проекта (там src/__init__.py с версией).
        name: ASCII-имя приложения (--name).
        os_tag: Метка ОС: macos, windows или linux.
        arch_tag: Метка архитектуры: universal, arm64, x86_64, x64.

    Returns:
        Имя без расширения, например «moscow-burials-0.1.0-windows-x64».

    Raises:
        ValueError: В src/__init__.py нет строки __version__.
    """
    text = (project_root / "src" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise ValueError("В src/__init__.py не найдена версия проекта (__version__)")
    return f"{name}-{match.group(1)}-{os_tag}-{arch_tag}"


def _make_release_zip(target: Path, archive_stem: str) -> Path:
    """Упаковывает только собранное приложение в zip-архив для публикации в Releases.

    Перед упаковкой удаляет служебные файлы ОС (.DS_Store, ._*, Thumbs.db, desktop.ini),
    чтобы они не попали в публичный архив. На macOS архив создаётся через ditto
    без resource fork и расширенных атрибутов (сохраняет симлинки и подпись .app).

    Args:
        target: Путь к собранному приложению (dist/<name>.app или папка dist/<name>).
        archive_stem: Имя архива без расширения (ASCII, для корректного имени в Releases).

    Returns:
        Путь к созданному zip-архиву рядом с target.
    """
    _remove_junk_files(target)

    archive_path = target.parent / f"{archive_stem}.zip"
    if archive_path.exists():
        archive_path.unlink()

    if sys.platform == "darwin":
        subprocess.run(
            [
                "ditto",
                "-c",
                "-k",
                "--keepParent",
                "--norsrc",
                "--noextattr",
                "--noacl",
                str(target),
                str(archive_path),
            ],
            check=True,
        )
    else:
        shutil.make_archive(
            str(target.parent / archive_stem),
            "zip",
            root_dir=str(target.parent),
            base_dir=target.name,
        )
    logger.info("Создан релизный архив: %s", archive_path)
    return archive_path


def _build_parser() -> argparse.ArgumentParser:
    """Создаёт парсер аргументов для скрипта сборки дистрибутива.

    Returns:
        Настроенный экземпляр argparse.ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="python -m scripts.build",
        description="Сборка автономного дистрибутива (PyInstaller --onedir / .app) для Windows/macOS/Linux",
    )
    parser.add_argument(
        "--name",
        default="moscow-burials",
        help="Имя выходного приложения (по умолчанию: moscow-burials)",
    )
    parser.add_argument(
        "--include-db",
        action="store_true",
        help=f"Скопировать существующую базу {DEFAULT_DB_PATH} внутрь собранного дистрибутива (по умолчанию выключено)",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Путь к файлу БД при использовании --include-db (по умолчанию: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help="Дополнительно упаковать только собранное приложение в dist/<name>-<os>.zip "
        "без служебных файлов ОС (для публикации в Releases)",
    )
    parser.add_argument(
        "--dmg",
        action="store_true",
        help="Только macOS: упаковать .app в оформленный образ диска "
        "dist/<name>-<версия>-macos-<архитектура>.dmg (для публикации в Releases; "
        "нужен пакет dmgbuild); сам .app после упаковки из dist/ удаляется",
    )
    parser.add_argument(
        "--target-arch",
        choices=MACOS_TARGET_ARCHES,
        default=MACOS_DEFAULT_TARGET_ARCH,
        help="Только macOS: целевая архитектура .app (по умолчанию: "
        f"{MACOS_DEFAULT_TARGET_ARCH}). Интерпретатор, которым запущена сборка, "
        "должен содержать эту архитектуру (universal2 — только с Python от python.org)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Выполняет упаковку приложения через PyInstaller в папку dist/<name> (и dist/<name>.app на macOS).

    Args:
        argv: Список аргументов командной строки (или None для sys.argv).

    Returns:
        Код завершения процесса (0 — успешно).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent
    entry_script = project_root / "scripts" / "web.py"

    if args.dmg and sys.platform == "darwin":
        try:
            import dmgbuild  # noqa: F401
        except ImportError:
            logger.error(
                "Для --dmg нужен пакет dmgbuild. Установите его командой: "
                "%s -m pip install \"dmgbuild==1.6.7\"",
                sys.executable,
            )
            return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        logger.error(
            "PyInstaller не установлен в текущем виртуальном окружении. "
            "Установите его командой: .venv/bin/pip install pyinstaller"
        )
        return 1

    sep = ";" if os.name == "nt" else ":"
    effective_name = (
        APP_DISPLAY_NAME
        if (os.name == "nt" and args.name == "moscow-burials")
        else args.name
    )
    tmp_dir = project_root / "tmp"
    work_dir = tmp_dir / "build"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        effective_name,
        "--workpath",
        str(work_dir),
        "--specpath",
        str(tmp_dir),
        "--paths",
        str(project_root),
        "--add-data",
        f"{project_root / 'src' / 'static'}{sep}{Path('src') / 'static'}",
    ]

    env = os.environ.copy()
    icns_path: Path | None = None
    if sys.platform == "darwin":
        env["MACOSX_DEPLOYMENT_TARGET"] = MACOS_DEPLOYMENT_TARGET
        cmd.extend(
            [
                "--windowed",
                "--osx-bundle-identifier",
                MACOS_BUNDLE_ID,
                "--target-arch",
                args.target_arch,
            ]
        )
        icns_path = _generate_macos_icns(
            project_root / "src" / "static" / "coat_of_arms.svg",
            tmp_dir,
            project_root / WINDOWS_ICO_RELPATH,
        )
        if icns_path is not None:
            cmd.extend(["--icon", str(icns_path)])
    elif os.name == "nt":
        cmd.append("--windowed")
        ico_path = project_root / WINDOWS_ICO_RELPATH
        if ico_path.is_file():
            cmd.extend(["--icon", str(ico_path)])
        else:
            logger.warning("Иконка %s не найдена, сборка продолжится без иконки.", ico_path)

    cmd.append(str(entry_script))

    logger.info("Запуск сборки PyInstaller: %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(project_root), env=env, check=False)
    if result.returncode != 0:
        return result.returncode

    dist_dir = project_root / "dist" / effective_name
    if args.include_db:
        src_db = (project_root / args.db).resolve()
        if not src_db.is_file():
            logger.warning(
                "Файл БД %s не найден, копирование пропущено.",
                src_db,
            )
        else:
            dst_db = dist_dir / "output" / DB_FILE_NAME
            dst_db.parent.mkdir(parents=True, exist_ok=True)
            logger.info("Копирование базы данных в дистрибутив: %s", dst_db)
            shutil.copy2(src_db, dst_db)

    if sys.platform == "darwin":
        raw_app_bundle = project_root / "dist" / f"{effective_name}.app"
        target_app_bundle = (
            project_root / "dist" / f"{MACOS_DISPLAY_NAME}.app"
            if args.name == "moscow-burials"
            else raw_app_bundle
        )
        if raw_app_bundle.is_dir():
            if target_app_bundle != raw_app_bundle:
                if target_app_bundle.exists():
                    shutil.rmtree(target_app_bundle)
                raw_app_bundle.rename(target_app_bundle)
            if dist_dir.is_dir():
                shutil.rmtree(dist_dir)
            _patch_and_sign_macos_app(target_app_bundle)
            problems = _verify_macos_bundle(target_app_bundle, args.target_arch)
            if problems:
                for problem in problems:
                    logger.error("Несовместимый файл: %s", problem)
                logger.error(
                    "Бандл %s не запустится на заявленных системах (macOS %s+, %s): "
                    "найдено проблем: %d. Соберите приложение интерпретатором Python, "
                    "собранным под эту архитектуру и версию macOS (не Homebrew).",
                    target_app_bundle,
                    MACOS_MIN_VERSION,
                    args.target_arch,
                    len(problems),
                )
                return 1
            logger.info(
                "Проверка совместимости пройдена: macOS %s+, %s",
                MACOS_MIN_VERSION,
                args.target_arch,
            )
            logger.info("Сборка macOS .app успешно завершена: %s", target_app_bundle)
            macos_stem = _release_stem(
                project_root, args.name, "macos", DMG_ARCH_SUFFIXES[args.target_arch]
            )
            if args.zip:
                _make_release_zip(target_app_bundle, macos_stem)
            if args.dmg:
                _make_release_dmg(
                    target_app_bundle,
                    target_app_bundle.parent / f"{macos_stem}.dmg",
                    icns_path,
                    project_root / DMG_BACKGROUND_SVG_RELPATH,
                )
                # Для публикации нужен только образ: .app внутри него, отдельная копия в dist/ лишняя.
                shutil.rmtree(target_app_bundle)
                logger.info("Бандл %s удалён: приложение есть в образе диска.", target_app_bundle.name)
            return 0

    logger.info("Сборка успешно завершена: %s", dist_dir)
    if args.dmg:
        logger.warning("Флаг --dmg действует только при сборке на macOS, пропущен.")
    if args.zip:
        if os.name == "nt":
            # Обычай Windows-релизов: x64 / x86 по разрядности интерпретатора сборки.
            os_tag, arch_tag = "windows", "x64" if struct.calcsize("P") == 8 else "x86"
        else:
            os_tag, arch_tag = "linux", platform.machine() or "unknown"
        _make_release_zip(dist_dir, _release_stem(project_root, args.name, os_tag, arch_tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
