"""Transparent access to raw capture files, compressed or not.

The WS collectors write data/<channel>/<sym>_<UTC date>.jsonl and never
rewrite a file once its UTC day rolls over. Completed days compress ~8.6x,
so compress_old_data.py gzips them in place.

Every reader must therefore accept both .jsonl and .jsonl.gz. A reader that
globs "*.jsonl" silently sees nothing for compressed days — no error, just
missing history — so route all raw-capture reads through here.
"""
from __future__ import annotations

import gzip
from pathlib import Path
from typing import IO, Iterable

GZ_SUFFIX = ".gz"


def open_data_file(path: Path, encoding: str = "utf-8", errors: str = "strict") -> IO[str]:
    """Open path, transparently falling back to its .gz twin.

    Accepts either the plain or the .gz path and opens whichever exists,
    preferring the uncompressed one when both are present (mid-compression).
    """
    path = Path(path)
    if path.suffix == GZ_SUFFIX:
        plain, gz = path.with_suffix(""), path
    else:
        plain, gz = path, path.with_suffix(path.suffix + GZ_SUFFIX)
    if plain.exists():
        return plain.open("r", encoding=encoding, errors=errors)
    if gz.exists():
        return gzip.open(gz, "rt", encoding=encoding, errors=errors)
    raise FileNotFoundError(f"neither {plain} nor {gz} exists")


def data_file_exists(path: Path) -> bool:
    """True when either the plain or the .gz form of path is present."""
    path = Path(path)
    if path.suffix == GZ_SUFFIX:
        return path.exists() or path.with_suffix("").exists()
    return path.exists() or path.with_suffix(path.suffix + GZ_SUFFIX).exists()


def iter_data_files(directory: Path, pattern: str) -> list[Path]:
    """Sorted matches for pattern, including .gz twins, one entry per day.

    pattern is a plain-suffix glob such as "btc_*.jsonl". When both forms
    exist for the same stem the uncompressed one wins.
    """
    directory = Path(directory)
    if not directory.exists():
        return []
    best: dict[str, Path] = {}
    for path in directory.glob(pattern):
        best[path.name] = path
    for path in directory.glob(pattern + GZ_SUFFIX):
        name = path.name[: -len(GZ_SUFFIX)]
        best.setdefault(name, path)
    return [best[k] for k in sorted(best)]


def iter_jsonl_lines(paths: Iterable[Path]) -> Iterable[str]:
    """Yield every line across paths, decompressing as needed."""
    for path in paths:
        with open_data_file(path, errors="replace") as fh:
            yield from fh
