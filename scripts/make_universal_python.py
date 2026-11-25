"""Склейка двух python-build-standalone (x86_64 и arm64) в один переносимый universal2 Python."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64 (little-endian)
    b"\xce\xfa\xed\xfe",  # MH_MAGIC (little-endian)
    b"\xca\xfe\xba\xbe",  # FAT_MAGIC
    b"\xca\xfe\xba\xbf",  # FAT_MAGIC_64
}


def _is_macho(path: Path) -> bool:
    """Проверяет по сигнатуре, является ли файл Mach-O бинарником.

    Args:
        path: Путь к проверяемому файлу.

    Returns:
        True, если файл начинается с магической сигнатуры Mach-O.
    """
    with path.open("rb") as fp:
        return fp.read(4) in MACHO_MAGICS


def merge_universal(x86_64_root: Path, arm64_root: Path, out_root: Path) -> int:
    """Собирает universal2-дерево: парные Mach-O склеиваются lipo, остальное копируется из x86_64.

    После lipo подпись недействительна, а arm64 не запустится без неё, поэтому
    все склеенные файлы переподписываются ad-hoc подписью.

    Args:
        x86_64_root: Распакованный python-build-standalone для x86_64 (папка python/).
        arm64_root: Распакованный python-build-standalone для aarch64 (папка python/).
        out_root: Папка результата (будет пересоздана).

    Returns:
        Количество склеенных Mach-O файлов.
    """
    if out_root.exists():
        shutil.rmtree(out_root)
    merged: list[Path] = []
    for root, dirs, files in os.walk(x86_64_root, followlinks=False):
        rel_root = Path(root).relative_to(x86_64_root)
        (out_root / rel_root).mkdir(parents=True, exist_ok=True)
        for name in dirs + files:
            src = Path(root) / name
            dst = out_root / rel_root / name
            if src.is_symlink():
                if not dst.is_symlink() and not dst.exists():
                    dst.symlink_to(os.readlink(src))
                continue
            if not src.is_file():
                continue
            other = arm64_root / rel_root / name
            if (
                _is_macho(src)
                and other.is_file()
                and not other.is_symlink()
                and _is_macho(other)
            ):
                subprocess.run(
                    ["lipo", "-create", str(src), str(other), "-output", str(dst)],
                    check=True,
                )
                shutil.copymode(src, dst)
                merged.append(dst)
            else:
                shutil.copy2(src, dst)
    for path in merged:
        subprocess.run(
            ["codesign", "--force", "--sign", "-", str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return len(merged)


def main(argv: list[str] | None = None) -> int:
    """Точка входа: python -m scripts.make_universal_python <x86_64> <arm64> <out>.

    Args:
        argv: Список аргументов командной строки (или None для sys.argv).

    Returns:
        Код завершения процесса (0 — успешно).
    """
    parser = argparse.ArgumentParser(
        prog="python -m scripts.make_universal_python",
        description="Склеивает python-build-standalone x86_64 и arm64 в universal2",
    )
    parser.add_argument("x86_64", type=Path, help="Папка python/ сборки x86_64")
    parser.add_argument("arm64", type=Path, help="Папка python/ сборки aarch64")
    parser.add_argument("out", type=Path, help="Папка результата")
    args = parser.parse_args(argv)
    count = merge_universal(args.x86_64, args.arm64, args.out)
    print(f"Склеено Mach-O файлов: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
