"""
HyphyLiquid - one-command system status.

Shows:
  - Background daemon status (poller + paper-trade loop)
  - Latest HyperPerps snapshot state for BTC + ETH
  - Paper trades + last signals
  - Tests passing
  - Last commit
  - Vault research notes

Use this to check on the project without having to remember where everything is.
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VAULT = Path(r"C:\Users\AbuBa\Documents\Obsidian Vault\projects\gold-silver-hyperliquid")
LOG_DIR = PROJECT_ROOT / "logs"


def _section(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def show_daemons() -> None:
    _section("DAEMONS")
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process pythonw -ErrorAction SilentlyContinue | "
         "Select-Object Id, StartTime, @{n='Cmd';e={$_.CommandLine.Substring(0, [Math]::Min(80, $_.CommandLine.Length))}} | "
         "Format-Table -AutoSize | Out-String"],
        capture_output=True, text=True,
    )
    print(out.stdout or "(none)")


def show_snapshots() -> None:
    _section("HYPERPERPS SNAPSHOTS (poller output)")
    snap_dir = PROJECT_ROOT / "data" / "hyperperps_snapshots"
    if not snap_dir.exists():
        print("  (no snapshot dir)")
        return
    for f in sorted(snap_dir.glob("*.jsonl")):
        lines = f.read_text(encoding="utf-8").strip().split("\n")
        print(f"  {f.name}: {len(lines)} snapshots, last: {f.stat().st_size:,} bytes")
    # Show last snapshot's metadata for BTC
    btc_path = snap_dir / "btc_2026-08-02.jsonl"
    if btc_path.exists():
        last = btc_path.read_text(encoding="utf-8").strip().split("\n")[-1]
        snap = json.loads(last)["snapshot"]
        meta = snap.get("_meta", {})
        cm = snap.get("cascade_mass", {})
        print(f"\n  Latest BTC: {meta.get('as_of', '?')} (age {meta.get('age_seconds', '?')}s)  "
              f"sample={snap.get('sample_size', '?')}  "
              f"spot=${snap.get('spot_at_compute', 0):,.0f}")
        print(f"    cascade 2%: long=${cm.get('long', {}).get('within_2pct', 0)/1e6:.1f}M  "
              f"short=${cm.get('short', {}).get('within_2pct', 0)/1e6:.1f}M")


def show_paper_trades() -> None:
    _section("PAPER TRADES")
    path = PROJECT_ROOT / "data" / "paper_trades.jsonl"
    if not path.exists():
        print("  (no paper trades log yet)")
        return
    trades = [json.loads(l) for l in path.read_text(encoding="utf-8").strip().split("\n") if l]
    if not trades:
        print("  (empty - market calm so far)")
        return
    print(f"  Total signals: {len(trades)}")
    last = trades[-1]
    print(f"  Last signal: {last['signal_ts']}  {last['symbol']}  {last['direction']}  conf={last['confidence']:.2f}")
    print(f"    {last['reason']}")


def show_tests() -> None:
    _section("TESTS")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    last_line = (r.stdout.strip().splitlines() or [""])[-1]
    print(f"  {last_line}")


def show_git() -> None:
    _section("GIT")
    r = subprocess.run(
        ["git", "log", "--oneline", "-5"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    print(r.stdout or "(no commits)")
    r = subprocess.run(
        ["git", "status", "--short"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    if r.stdout.strip():
        print(f"\n  Uncommitted: {len(r.stdout.strip().splitlines())} files")
    else:
        print("\n  Working tree clean.")


def show_vault() -> None:
    _section("VAULT RESEARCH NOTES")
    if not VAULT.exists():
        print("  (vault not found)")
        return
    for sub in ("research", "notes"):
        d = VAULT / sub
        if not d.exists():
            continue
        files = sorted(d.glob("*.md"))
        print(f"  {sub}/: {len(files)} notes")
        for f in files[-5:]:
            print(f"    {f.name}")


def show_data() -> None:
    _section("DATA")
    data_dir = PROJECT_ROOT / "data"
    for f in sorted(data_dir.glob("*")):
        if f.is_file():
            print(f"  {f.name}  ({f.stat().st_size:,} bytes)")
        elif f.is_dir():
            children = list(f.glob("*"))
            print(f"  {f.name}/  ({len(children)} files)")


def main() -> int:
    print(f"HyphyLiquid Status  -  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    show_daemons()
    show_snapshots()
    show_paper_trades()
    show_tests()
    show_data()
    show_git()
    show_vault()
    return 0


if __name__ == "__main__":
    sys.exit(main())
