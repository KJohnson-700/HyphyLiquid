"""Trigger logic for the cascade-rebuild + backtest cycle.

Decides when to run scripts/build_cascades.py and
scripts/run_fade_or_follow_backtest.py based on:
  - liquidations.jsonl has 150+ mature new rows since last rebuild
  - mature means the event is at least 30 minutes old
  - last rebuild was at least 60 minutes ago
  - daily fallback at 00:15 PT

State is persisted at data/.rebuild_baseline.json (auto-written after
each successful run by scripts/run_rebuild_cycle.py).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

# --- Paths ---
REPO_ROOT = Path(__file__).resolve().parents[2]
LIQUIDATIONS_PATH = REPO_ROOT / "data" / "liquidations.jsonl"
BASELINE_PATH = REPO_ROOT / "data" / ".rebuild_baseline.json"

# --- Thresholds (from Slim) ---
THRESHOLD_NEW_ROWS = 150
THRESHOLD_LAST_LIQ_AGE_MIN = 30
THRESHOLD_LAST_REBUILD_AGE_MIN = 60

# --- Daily fallback (00:15 PT) ---
DAILY_FALLBACK_HOUR_PT = 0
DAILY_FALLBACK_MIN_PT = 15
DAILY_FALLBACK_WINDOW_MIN = 30  # window after :15 in which we'll fire
PT_UTC_OFFSET = timedelta(hours=-7)  # PDT (Aug 2026)


# ----------------------------------------------------------------------------
# Timestamp parsing (handles ISO 8601 strings, Unix ms, Unix seconds)
# ----------------------------------------------------------------------------

def _parse_ts_string(value: Any) -> Optional[int]:
    """Convert a timestamp value to Unix milliseconds. Returns None on failure."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        # Heuristic: > 1e12 means already ms, else seconds.
        return int(n) if n > 1e12 else int(n * 1000)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # ISO 8601 (with optional 'Z' suffix)
        iso = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            pass
        # Numeric string (Unix ms or seconds)
        try:
            n = float(s)
            return int(n) if n > 1e12 else int(n * 1000)
        except ValueError:
            pass
    return None


def parse_liquidation_ts(line: str) -> Optional[int]:
    """Parse the timestamp of a single liquidation JSONL line.

    Looks for the timestamp at common locations:
      - top-level: ts, t, time, recv_ts, timestamp
      - nested: payload.{ts,t,time,T}, data.{ts,t,time,T}

    Returns Unix milliseconds, or None if not found.
    """
    try:
        obj = json.loads(line)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None

    # Top-level common fields
    for key in ("ts", "t", "time", "recv_ts", "timestamp"):
        if key in obj:
            ms = _parse_ts_string(obj[key])
            if ms is not None:
                return ms

    # Nested in 'payload' or 'data'
    for wrapper in ("payload", "data"):
        inner = obj.get(wrapper)
        if isinstance(inner, dict):
            for key in ("ts", "t", "T", "time"):
                if key in inner:
                    ms = _parse_ts_string(inner[key])
                    if ms is not None:
                        return ms

    return None


# ----------------------------------------------------------------------------
# Liquidation file helpers
# ----------------------------------------------------------------------------

def count_liquidations(path: Path = LIQUIDATIONS_PATH) -> int:
    """Return current line count of data/liquidations.jsonl (skips blank lines)."""
    if not path.exists():
        return 0
    n = 0
    with open(path, "rb") as f:
        for raw in f:
            if raw.strip():
                n += 1
    return n


def last_liquidation_ts_ms(path: Path = LIQUIDATIONS_PATH) -> Optional[int]:
    """Return the timestamp (Unix ms) of the most recent liquidation line."""
    if not path.exists():
        return None
    last_ms: Optional[int] = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            ms = parse_liquidation_ts(line)
            if ms is not None:
                last_ms = ms
    return last_ms


def count_mature_new_liquidations(
    path: Path,
    baseline_count: int,
    mature_before_ms: int,
) -> int:
    """Count rows after baseline_count whose event timestamp is mature.

    Live liquidation streams may keep appending fresh events. Requiring the
    latest event in the whole file to be 30+ minutes old can starve rebuilds in
    active markets, so the trigger instead counts only the new rows that
    already have enough future candle coverage.
    """
    if not path.exists():
        return 0
    mature = 0
    row_num = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row_num += 1
            if row_num <= baseline_count:
                continue
            ms = parse_liquidation_ts(line)
            if ms is not None and ms <= mature_before_ms:
                mature += 1
    return mature


# ----------------------------------------------------------------------------
# Baseline file (data/.rebuild_baseline.json)
# ----------------------------------------------------------------------------

def load_baseline(path: Path = BASELINE_PATH) -> dict:
    """Load the baseline file. Returns empty dict if missing or corrupt."""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (ValueError, json.JSONDecodeError, OSError):
        return {}


def save_baseline(payload: dict, path: Path = BASELINE_PATH) -> None:
    """Write the baseline file atomically (tmp + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _baseline_rebuild_dt(baseline: dict) -> Optional[datetime]:
    raw = baseline.get("last_rebuild_ts")
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ----------------------------------------------------------------------------
# Trigger decision
# ----------------------------------------------------------------------------

def _now_pt(now_utc: datetime) -> datetime:
    return now_utc + PT_UTC_OFFSET


def check_should_rebuild(
    now_utc: Optional[datetime] = None,
    liquidations_path: Path = LIQUIDATIONS_PATH,
    baseline_path: Path = BASELINE_PATH,
) -> Tuple[bool, dict]:
    """Decide whether to fire build_cascades + backtest.

    Returns (should_fire, info) where info contains the trigger breakdown.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    info: dict[str, Any] = {
        "new_rows": 0,
        "mature_new_rows": 0,
        "last_liq_age_min": None,
        "last_rebuild_age_min": None,
        "daily_fallback": False,
        "reasons": [],
        "current_liquidation_count": count_liquidations(liquidations_path),
    }

    last_liq_ms = last_liquidation_ts_ms(liquidations_path)
    if last_liq_ms is None:
        info["reasons"].append("no liquidations found")
        return False, info
    last_liq_dt = datetime.fromtimestamp(last_liq_ms / 1000.0, tz=timezone.utc)
    info["last_liq_age_min"] = (now_utc - last_liq_dt).total_seconds() / 60.0

    baseline = load_baseline(baseline_path)
    last_rebuild_dt = _baseline_rebuild_dt(baseline)
    if last_rebuild_dt is not None:
        info["last_rebuild_age_min"] = (now_utc - last_rebuild_dt).total_seconds() / 60.0

    base_count = baseline.get("liquidation_count")
    if isinstance(base_count, int):
        info["new_rows"] = info["current_liquidation_count"] - base_count
        mature_before = now_utc - timedelta(minutes=THRESHOLD_LAST_LIQ_AGE_MIN)
        info["mature_new_rows"] = count_mature_new_liquidations(
            liquidations_path,
            base_count,
            int(mature_before.timestamp() * 1000),
        )

    # --- Daily fallback check ---
    now_pt = _now_pt(now_utc)
    daily_window_start = now_pt.replace(
        hour=DAILY_FALLBACK_HOUR_PT,
        minute=DAILY_FALLBACK_MIN_PT,
        second=0,
        microsecond=0,
    )
    daily_window_end = daily_window_start + timedelta(minutes=DAILY_FALLBACK_WINDOW_MIN)
    in_daily_window = daily_window_start <= now_pt < daily_window_end
    last_daily_date = baseline.get("last_daily_fallback_date")
    today_pt = now_pt.date().isoformat()
    daily_already_fired = (last_daily_date == today_pt)

    if in_daily_window and not daily_already_fired:
        info["daily_fallback"] = True
        info["reasons"].append(f"daily fallback ({today_pt} 00:15 PT)")
        return True, info

    # --- Standard trigger ---
    if not baseline:
        info["reasons"].append("no baseline yet (initial run pending)")
        return False, info

    if info["mature_new_rows"] < THRESHOLD_NEW_ROWS:
        info["reasons"].append(
            f"mature_new_rows {info['mature_new_rows']} < {THRESHOLD_NEW_ROWS}"
        )
    if info["last_rebuild_age_min"] is None or info["last_rebuild_age_min"] < THRESHOLD_LAST_REBUILD_AGE_MIN:
        info["reasons"].append(
            f"last_rebuild_age {info['last_rebuild_age_min']} < {THRESHOLD_LAST_REBUILD_AGE_MIN} min"
        )

    should_fire = (
        info["mature_new_rows"] >= THRESHOLD_NEW_ROWS
        and info["last_rebuild_age_min"] is not None
        and info["last_rebuild_age_min"] >= THRESHOLD_LAST_REBUILD_AGE_MIN
    )
    return should_fire, info


def update_baseline(
    liquidations_path: Path = LIQUIDATIONS_PATH,
    baseline_path: Path = BASELINE_PATH,
    now_utc: Optional[datetime] = None,
) -> dict:
    """Write the current state to the baseline file. Returns the payload."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    current_count = count_liquidations(liquidations_path)
    last_liq_ms = last_liquidation_ts_ms(liquidations_path)
    last_liq_iso = (
        datetime.fromtimestamp(last_liq_ms / 1000.0, tz=timezone.utc).isoformat()
        if last_liq_ms is not None
        else None
    )

    baseline = load_baseline(baseline_path)

    # Mark daily fallback date if we're firing during the daily window
    now_pt = _now_pt(now_utc)
    in_daily_window = (
        now_pt.hour == DAILY_FALLBACK_HOUR_PT
        and DAILY_FALLBACK_MIN_PT
        <= now_pt.minute
        < DAILY_FALLBACK_MIN_PT + DAILY_FALLBACK_WINDOW_MIN
    )
    if in_daily_window:
        baseline["last_daily_fallback_date"] = now_pt.date().isoformat()

    baseline["liquidation_count"] = current_count
    baseline["last_rebuild_ts"] = now_utc.isoformat()
    baseline["last_liquidation_ts"] = last_liq_iso
    save_baseline(baseline, baseline_path)
    return baseline


# ----------------------------------------------------------------------------
# CLI helper
# ----------------------------------------------------------------------------

def main() -> int:  # pragma: no cover
    """Print a human-readable trigger report and exit 0 if should_fire else 1."""
    should_fire, info = check_should_rebuild()
    print(f"current_liquidation_count: {info['current_liquidation_count']}")
    print(f"new_rows_since_rebuild:    {info['new_rows']} (need >= {THRESHOLD_NEW_ROWS})")
    print(f"mature_new_rows:           {info['mature_new_rows']} (need >= {THRESHOLD_NEW_ROWS})")
    last_liq = info['last_liq_age_min']
    print(f"last_liq_age_min:          {last_liq:.1f}" if last_liq is not None else "last_liq_age_min:          None")
    last_rebuild = info['last_rebuild_age_min']
    print(f"last_rebuild_age_min:      {last_rebuild:.1f}" if last_rebuild is not None else "last_rebuild_age_min:      None")
    print(f"daily_fallback_active:     {info['daily_fallback']}")
    print(f"reasons:                   {info['reasons'] or 'all conditions met'}")
    print(f"--> {'FIRE' if should_fire else 'HOLD'}")
    return 0 if should_fire else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
