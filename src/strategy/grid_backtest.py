"""Research-only event-anchored range grid backtest.

This is not a classic always-on grid. It only tests whether liquidation sweeps
inside confirmed range regimes can be harvested with a small bounded basket.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean, median

from src.strategy.fade_or_follow_backtest import _bar_ts, _fade_direction, _return_pct, find_entry_idx
from src.strategy.event_features import _canonical_symbol
from src.strategy.lane_backtest import _close, _high, _low, bollinger_at
from src.strategy.regime import band_width_bucket

GRID_RESEARCH_SYMBOLS = {"SOL", "HYPE", "DOGE", "BNB", "xyz:GOLD", "xyz:SILVER"}


@dataclass(frozen=True)
class GridConfig:
    """Bounded grid lane parameters."""

    band_period: int = 20
    stdev_mult: float = 2.0
    allowed_band_buckets: tuple[str, ...] = ("normal", "wide")
    grid_spacing_bps: float = 10.0
    max_levels: int = 3
    stop_buffer_bps: float = 10.0
    max_hold_minutes: int = 60
    max_entry_lag_minutes: int = 2
    round_trip_cost_bps: float = 8.0


@dataclass(frozen=True)
class GridTrade:
    """One simulated bounded grid basket."""

    lane: str
    cascade_start_ts: str
    symbol: str
    side: str
    direction: str
    entry_ts: str
    exit_ts: str
    avg_entry_price: float
    exit_price: float
    levels_filled: int
    gross_return_pct: float
    net_return_pct: float
    bars_held: int
    band_mid: float
    band_upper: float
    band_lower: float
    band_width_pct: float
    band_width_bucket: str
    grid_spacing_bps: float
    stop_price: float
    target_price: float
    exit_reason: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _price_with_bps(price: float, direction: str, bps: float) -> float:
    pct = bps / 10_000.0
    if direction == "long":
        return price * (1.0 - pct)
    return price * (1.0 + pct)


def _grid_level_prices(entry: float, direction: str, spacing_bps: float, max_levels: int) -> list[float]:
    return [_price_with_bps(entry, direction, spacing_bps * i) for i in range(max(1, max_levels))]


def _range_sweep_confirmed(side: str, bar: dict, bands: dict) -> bool:
    high = _high(bar)
    low = _low(bar)
    close = _close(bar)
    if high is None or low is None or close is None:
        return False
    # B-side liquidations tend to be upside pressure in this project's
    # current lane framing, so the fade/grid is short from the upper band.
    if side == "B":
        return high >= bands["upper"] and close <= high
    if side == "A":
        return low <= bands["lower"] and close >= low
    return False


def _stop_price(direction: str, bands: dict, stop_buffer_bps: float) -> float:
    buffer = stop_buffer_bps / 10_000.0
    if direction == "long":
        return bands["lower"] * (1.0 - buffer)
    return bands["upper"] * (1.0 + buffer)


def _level_filled(direction: str, price: float, high: float, low: float) -> bool:
    if direction == "long":
        return low <= price
    return high >= price


def _basket_exit_hit(direction: str, target: float, stop: float, high: float, low: float) -> str | None:
    # Stop wins over target on the same bar for conservative simulation.
    if direction == "long":
        if low <= stop:
            return "range_stop"
        if high >= target:
            return "mid_band_target"
    else:
        if high >= stop:
            return "range_stop"
        if low <= target:
            return "mid_band_target"
    return None


def simulate_grid_trade(
    cascade: dict,
    candles: list[dict],
    entry_idx: int,
    config: GridConfig,
) -> GridTrade | None:
    """Simulate one bounded grid basket for a single cascade."""
    side = str(cascade.get("side", ""))
    symbol = _canonical_symbol(str(cascade.get("symbol", "")))
    if symbol not in GRID_RESEARCH_SYMBOLS:
        return None
    if side not in {"A", "B"} or entry_idx >= len(candles):
        return None
    bands = bollinger_at(candles, entry_idx, config.band_period, config.stdev_mult)
    if not bands:
        return None
    bucket = band_width_bucket(float(bands["width_pct"]))
    if bucket not in set(config.allowed_band_buckets):
        return None
    entry_bar = candles[entry_idx]
    if not _range_sweep_confirmed(side, entry_bar, bands):
        return None
    entry_price = _close(entry_bar)
    if entry_price is None or entry_price <= 0:
        return None

    direction = _fade_direction(side)
    target = float(bands["mid"])
    stop = _stop_price(direction, bands, config.stop_buffer_bps)
    if direction == "long" and not (stop < entry_price < target):
        return None
    if direction == "short" and not (target < entry_price < stop):
        return None

    levels = _grid_level_prices(entry_price, direction, config.grid_spacing_bps, config.max_levels)
    filled: list[float] = [levels[0]]
    exit_idx = min(entry_idx + config.max_hold_minutes, len(candles) - 1)
    exit_price = _close(candles[exit_idx])
    exit_reason = "timeout"

    for idx in range(entry_idx + 1, exit_idx + 1):
        high = _high(candles[idx])
        low = _low(candles[idx])
        close = _close(candles[idx])
        if high is None or low is None or close is None:
            continue
        for level in levels[len(filled):]:
            if _level_filled(direction, level, high, low):
                filled.append(level)
            else:
                break
        hit = _basket_exit_hit(direction, target, stop, high, low)
        if hit is not None:
            exit_idx = idx
            exit_reason = hit
            exit_price = stop if hit == "range_stop" else target
            break
        exit_price = close

    if exit_price is None:
        return None
    avg_entry = mean(filled)
    gross = _return_pct(direction, avg_entry, exit_price)
    net = gross - (config.round_trip_cost_bps / 100.0)
    return GridTrade(
        lane="event_range_grid",
        cascade_start_ts=str(cascade.get("start_ts", cascade.get("event_ts", ""))),
        symbol=symbol,
        side=side,
        direction=direction,
        entry_ts=_bar_ts(candles[entry_idx]),
        exit_ts=_bar_ts(candles[exit_idx]),
        avg_entry_price=round(avg_entry, 8),
        exit_price=round(exit_price, 8),
        levels_filled=len(filled),
        gross_return_pct=round(gross, 4),
        net_return_pct=round(net, 4),
        bars_held=max(0, exit_idx - entry_idx),
        band_mid=round(float(bands["mid"]), 8),
        band_upper=round(float(bands["upper"]), 8),
        band_lower=round(float(bands["lower"]), 8),
        band_width_pct=round(float(bands["width_pct"]), 4),
        band_width_bucket=bucket,
        grid_spacing_bps=config.grid_spacing_bps,
        stop_price=round(stop, 8),
        target_price=round(target, 8),
        exit_reason=exit_reason,
        reason=f"{bucket} range sweep grid toward mid-band",
    )


def run_event_range_grid(
    cascades: list[dict],
    candles_by_symbol: dict[str, list[dict]],
    config: GridConfig | None = None,
) -> list[GridTrade]:
    """Run the research-only event range grid over cascades."""
    cfg = config or GridConfig()
    trades: list[GridTrade] = []
    for cascade in cascades:
        symbol = _canonical_symbol(str(cascade.get("symbol", "")))
        if symbol not in GRID_RESEARCH_SYMBOLS:
            continue
        candles = candles_by_symbol.get(symbol)
        if not candles:
            continue
        start_ts = cascade.get("start_ts", cascade.get("event_ts"))
        if not start_ts:
            continue
        entry_idx = find_entry_idx(candles, str(start_ts), cfg.max_entry_lag_minutes)
        if entry_idx is None:
            continue
        trade = simulate_grid_trade(cascade, candles, entry_idx, cfg)
        if trade is not None:
            trades.append(trade)
    return trades


def _profit_factor(values: list[float]) -> float | str:
    wins = sum(v for v in values if v > 0)
    losses = abs(sum(v for v in values if v < 0))
    if losses == 0:
        return "inf" if wins > 0 else 0.0
    return round(wins / losses, 4)


def summarize_grid_trades(trades: list[GridTrade]) -> dict[str, dict]:
    """Summarize grid trades by symbol and band bucket."""
    buckets: dict[str, list[GridTrade]] = {}
    for trade in trades:
        for key in (
            f"symbol={trade.symbol}",
            f"symbol={trade.symbol}|band_width={trade.band_width_bucket}",
            f"symbol={trade.symbol}|side={trade.side}",
            f"symbol={trade.symbol}|exit={trade.exit_reason}",
        ):
            buckets.setdefault(key, []).append(trade)
    out: dict[str, dict] = {}
    for key, rows in buckets.items():
        returns = [r.net_return_pct for r in rows]
        wins = [r for r in rows if r.net_return_pct > 0]
        out[key] = {
            "bucket": key,
            "n": len(rows),
            "win_rate": round(len(wins) / len(rows), 4) if rows else 0.0,
            "avg_net_return_pct": round(mean(returns), 4) if returns else 0.0,
            "median_net_return_pct": round(median(returns), 4) if returns else 0.0,
            "profit_factor": _profit_factor(returns),
            "avg_levels_filled": round(mean([r.levels_filled for r in rows]), 4),
        }
    return out
