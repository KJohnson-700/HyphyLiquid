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
from datetime import datetime, timezone
from pathlib import Path

# Auto-backtest guard (matches cron self-tick thresholds in main session)
BACKTEST_GUARD_EVENTS = 100
BACKTEST_GUARD_HOURS = 24

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


def _bar(pct: float, width: int = 30) -> str:
    """ASCII progress bar. pct in [0, 100]."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * width))
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct:5.1f}%"


def _hms(seconds: float) -> str:
    """Format seconds as Hh MMm (signed)."""
    sign = "-" if seconds < 0 else ""
    s = int(abs(seconds))
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h > 0:
        return f"{sign}{h}h {m:02d}m"
    return f"{sign}{m}m"


def show_backtest_readiness() -> None:
    """Show progress toward the auto-backtest guard (100 events AND 24h old)."""
    _section("BACKTEST READINESS")
    path = PROJECT_ROOT / "data" / "liquidations.jsonl"
    if not path.exists():
        print("  (no liquidations file yet)")
        return
    events: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # defensive: skip corrupt lines the monitor already logged
                continue
    n = len(events)
    if n == 0:
        print("  (0 events captured)")
        return

    # Count gate
    count_pct = min(100.0, n / BACKTEST_GUARD_EVENTS * 100.0)
    count_ok = n >= BACKTEST_GUARD_EVENTS

    # Time gate: age of OLDEST event
    oldest_ts = datetime.fromisoformat(events[0]["ts"])
    if oldest_ts.tzinfo is None:
        oldest_ts = oldest_ts.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - oldest_ts).total_seconds() / 3600.0
    time_pct = min(100.0, age_hours / BACKTEST_GUARD_HOURS * 100.0)
    time_ok = age_hours >= BACKTEST_GUARD_HOURS

    # Earliest unlock = max(first event + 24h, (BACKTEST_GUARD_EVENTS - n) / rate + now)
    # Use the simple version: time gate depends on first event, count gate is now
    unlock_ts = oldest_ts.timestamp() + BACKTEST_GUARD_HOURS * 3600
    unlock_dt = datetime.fromtimestamp(unlock_ts, tz=timezone.utc)
    seconds_to_unlock = unlock_ts - datetime.now(timezone.utc).timestamp()

    # Throughput (last hour) so user can see if monitor is alive
    last_1h = sum(
        1 for e in events
        if (datetime.now(timezone.utc) - _parse_ts(e["ts"])).total_seconds() <= 3600
    )

    print(f"  Events:    {n}/{BACKTEST_GUARD_EVENTS}  (last 1h: +{last_1h})")
    print(f"    {_bar(count_pct)}  {'READY' if count_ok else 'waiting'}")
    print()
    print(f"  Age:       {age_hours:5.2f}h / {BACKTEST_GUARD_HOURS}h  (oldest: {oldest_ts.strftime('%H:%M:%S UTC')})")
    print(f"    {_bar(time_pct)}  {'READY' if time_ok else 'waiting'}")
    print()
    if count_ok and time_ok:
        print("  >>> AUTO-BACKTEST SHOULD HAVE RUN. Check cron output. <<<")
    else:
        bits = []
        if not count_ok:
            need = BACKTEST_GUARD_EVENTS - n
            rate = max(last_1h, 1)  # assume at least 1/h to keep ETA sane
            bits.append(f"~{need // max(rate, 1)}h to {BACKTEST_GUARD_EVENTS} events (at {rate}/h)")
        if not time_ok:
            bits.append(f"{_hms(seconds_to_unlock)} until unlock ({unlock_dt.strftime('%a %H:%M UTC')})")
        print("  Next gate: " + " | ".join(bits))


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> int:
    print(f"HyphyLiquid Status  -  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    show_daemons()
    show_snapshots()
    show_paper_trades()
    show_backtest_readiness()
    show_tests()
    show_data()
    show_git()
    show_vault()
    return 0


if __name__ == "__main__":
    sys.exit(main())
