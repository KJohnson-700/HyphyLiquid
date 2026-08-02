---
project: HyphyLiquid
topic: order-manager-hard-parts
date: 2026-08-01
status: handoff
owner: Codex
delegate: Mavis
---

# Order Manager Hard Parts Handoff

## What Codex Took Over

Codex reviewed `src/execution/order_manager.py` against the order manager review brief and patched the parts that can create real execution risk.

Changed files:

- `src/execution/order_manager.py`
- `tests/test_order_manager.py`

## Safety Changes Made

1. `NO_TRADE` signals are now rejected explicitly.
   - Before: anything not `LONG` became a short.
   - After: `SignalDirection.NO_TRADE` returns a rejected `OrderResult`.

2. ATR fallback trading was removed.
   - Before: if candles were empty or too short, ATR became `current_price * 0.005`.
   - After: the order manager refuses to trade without real ATR history.

3. Default order grouping changed to `normalTpsl`.
   - Reason: this flow places a parent entry with child TP/SL orders.
   - `positionTpsl` is for TP/SL tied to the whole existing position and still needs explicit testnet verification before any use.

4. Orphan entry handling was added.
   - If the entry order is live but a child TP/SL order errors, the manager attempts `bulk_cancel` on the entry.
   - The returned `OrderResult` sets `status="orphan_error"` and `needs_reconciliation=True`.

5. `OrderResult` now carries richer execution state.
   - Added `status`, `size_coin`, `needs_reconciliation`, `cancel_attempted`, per-leg statuses, and cancel response.

6. Size rounding now tries Hyperliquid `meta()` first.
   - Falls back to hardcoded BTC/ETH/SOL decimals only if metadata is unavailable.

## Tests Added

Added coverage for:

- `NO_TRADE` rejected rather than treated as short.
- Missing ATR history refuses to trade and does not call the exchange.
- Entry orphan path attempts cancel when child TP/SL order fails.
- Default grouping expectation changed to `normalTpsl`.

## Verification Status

Could not run the pytest suite because the repo venv points to a missing Windows Store Python path:

```powershell
venv\Scripts\python.exe -m pytest tests\test_order_manager.py -q
```

Failure:

```text
No Python at "C:\Users\AbuBa\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.10_qbz5n2kfra8p0\python.exe"
```

Codex did run syntax verification with the available NotebookLM tool Python:

```powershell
.tools\notebooklm-cli\Scripts\python.exe -m py_compile src\execution\order_manager.py tests\test_order_manager.py
```

Result: passed.

## Delegated To Mavis

Mavis may do these lower-risk tasks:

1. Rebuild the project venv with a real Python 3.10+ executable.
2. Run `pytest tests/test_order_manager.py -q`.
3. Run the full test suite after the venv is repaired.
4. Update vault/repo indexes so this handoff is linked from research.
5. Run a read-only testnet preflight.
6. Prepare a testnet-only script that submits one tiny far-from-market parent order with TP/SL, then cancels it.

## Mavis Must Not Do

Mavis must not:

- Place mainnet orders.
- Increase risk limits.
- Re-enable ATR fallback trading.
- Switch back to `positionTpsl` without a recorded testnet proof.
- Add ML, DCA, martingale, new assets, or new venues.
- Treat `filled=True` as final account truth without checking order/fill reconciliation.

## Remaining Hard Questions For Slim/Codex

1. Confirm on Hyperliquid testnet whether `normalTpsl` is the exact grouping for parent entry plus TP/SL children.
2. Decide how to persist `RiskState` across process restarts.
3. Add authenticated reconciliation before live canary:
   - `openOrders`
   - `clearinghouseState`
   - `userFills`
   - `orderUpdates`
4. Decide whether isolated margin becomes a hard v1 execution rule.

## Current Verdict

The order manager is safer than before, but it is still **not live-ready**. It is ready for repaired-unit-test validation and then a tiny testnet-only grouped-order proof.
