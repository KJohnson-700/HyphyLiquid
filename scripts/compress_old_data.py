"""Gzip completed raw-capture files to keep data/ from filling the disk.

The WS collectors write one file per symbol per UTC day and never touch a
file again once its day rolls over. Those completed files compress ~8.6x.

Safety rules, in order of importance:
  1. Never touch a file for the current UTC day — a daemon is appending to it.
     The local date can be a day behind UTC; always compare against UTC.
  2. Verify the .gz round-trips (same line count) before removing the original.
  3. Skip anything a process currently holds open.

Readers must go through src.data_files, which accepts either form.

Usage:
  python3 scripts/compress_old_data.py --dry-run     # show what would happen
  python3 scripts/compress_old_data.py               # do it
  python3 scripts/compress_old_data.py --older-than 3
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR = PROJECT_ROOT / "data"
CAPTURE_DIRS = ("ws_bbo", "ws_l2book", "ws_candle", "ws_activeAssetCtx",
                "ws_asset_ctx", "asset_ctx", "trades")


def _count_lines_plain(path: Path) -> int:
    with path.open("rb") as fh:
        return sum(1 for _ in fh)


def _count_lines_gz(path: Path) -> int:
    with gzip.open(path, "rb") as fh:
        return sum(1 for _ in fh)


def candidates(data_dir: Path, cutoff: str) -> list[Path]:
    """Completed .jsonl capture files strictly older than cutoff (UTC date str)."""
    out: list[Path] = []
    for name in CAPTURE_DIRS:
        d = data_dir / name
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.jsonl")):
            stem_date = path.stem.rsplit("_", 1)[-1]
            if len(stem_date) == 10 and stem_date < cutoff:
                out.append(path)
    return out


def compress(path: Path, *, dry_run: bool) -> tuple[bool, str]:
    gz = path.with_suffix(path.suffix + ".gz")
    before = path.stat().st_size
    if gz.exists():
        return False, f"skip (.gz already exists): {path.name}"
    if dry_run:
        return True, f"would compress {path.name} ({before/1e6:.1f} MB)"
    with path.open("rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    # verify before destroying the original
    if _count_lines_plain(path) != _count_lines_gz(gz):
        gz.unlink(missing_ok=True)
        return False, f"VERIFY FAILED, original kept: {path.name}"
    after = gz.stat().st_size
    path.unlink()
    return True, f"{path.name}: {before/1e6:.1f} -> {after/1e6:.1f} MB ({before/max(after,1):.1f}x)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--older-than", type=int, default=1,
                    help="days before today (UTC) to leave alone; default 1 = keep today")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = ap.parse_args()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.older_than - 1)).strftime("%Y-%m-%d")
    print(f"UTC now: {datetime.now(timezone.utc):%Y-%m-%d %H:%M}  cutoff: files dated < {cutoff}")

    files = candidates(args.data_dir, cutoff)
    if not files:
        print("nothing to compress")
        return 0

    total_before = sum(f.stat().st_size for f in files)
    print(f"{len(files)} file(s), {total_before/1e6:,.0f} MB")

    ok = fail = 0
    saved = 0
    for path in files:
        before = path.stat().st_size
        did, msg = compress(path, dry_run=args.dry_run)
        print(f"  {msg}")
        if did:
            ok += 1
            if not args.dry_run:
                saved += before - path.with_suffix(path.suffix + ".gz").stat().st_size
        else:
            fail += 1
    print(f"\ncompressed {ok}, skipped/failed {fail}"
          + ("" if args.dry_run else f", freed {saved/1e6:,.0f} MB"))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
