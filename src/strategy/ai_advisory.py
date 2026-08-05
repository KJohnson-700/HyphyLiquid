"""AI advisory contract for regime/tape assistance.

This module is deliberately outside the hot execution path. An AI model may
summarize context, flag disagreement, or recommend one of the pre-approved
playbooks, but it cannot create orders, override risk, alter leverage, or
promote research symbols into execution scope.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from src.strategy.regime import RESEARCH_SYMBOLS, V1_TRADE_SYMBOLS

VALID_ADVISORY_ACTIONS = frozenset({"stand_down", "maintain", "paper_only", "watch_playbook"})
VALID_PLAYBOOKS = frozenset(
    {
        "btc_b_failed_reclaim_ask_heavy",
        "hype_b_range_scalp_research",
        "eth_rejected_collect_only",
        "alts_collect_only",
    }
)
REQUIRED_EVIDENCE_KEYS = frozenset({"regime", "tape", "risk"})


@dataclass(frozen=True)
class AdvisoryPacket:
    """Bounded context packet prepared for an AI model."""

    packet_ts: str
    symbol: str
    scope: str
    deterministic_route: dict[str, Any]
    indicators: dict[str, Any]
    disagreement: dict[str, Any]
    tape: dict[str, Any]
    risk: dict[str, Any]
    news: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AdvisoryDecision:
    """Validated AI advisory decision.

    This is advice for paper/human review. It is not an executable order.
    """

    decision_ts: str
    symbol: str
    action: str
    playbook: str | None
    confidence: float
    rationale: str
    evidence: dict[str, Any]
    allowed_for_execution: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["warnings"] = list(self.warnings)
        return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _symbol_scope(symbol: str) -> str:
    sym = symbol.upper()
    if sym in V1_TRADE_SYMBOLS:
        return "v1"
    if sym in {s.upper() for s in RESEARCH_SYMBOLS}:
        return "research"
    return "unknown"


def make_advisory_packet(
    *,
    symbol: str,
    deterministic_route: dict[str, Any],
    indicators: dict[str, Any],
    disagreement: dict[str, Any] | None = None,
    tape: dict[str, Any] | None = None,
    risk: dict[str, Any] | None = None,
    news: dict[str, Any] | None = None,
) -> AdvisoryPacket:
    """Create a bounded packet that an AI model may review."""
    return AdvisoryPacket(
        packet_ts=_utc_now(),
        symbol=symbol.upper(),
        scope=_symbol_scope(symbol),
        deterministic_route=dict(deterministic_route),
        indicators=dict(indicators),
        disagreement=dict(disagreement or {}),
        tape=dict(tape or {}),
        risk=dict(risk or {}),
        news=dict(news or {}),
    )


def validate_advisory(packet: AdvisoryPacket, raw: dict[str, Any]) -> AdvisoryDecision:
    """Validate AI advice against project guardrails.

    The validator is fail-closed: malformed, overreaching, or under-evidenced
    advice becomes `stand_down` and is never execution-allowed.
    """
    warnings: list[str] = []
    symbol = str(raw.get("symbol") or packet.symbol).upper()
    if symbol != packet.symbol:
        warnings.append(f"symbol mismatch: packet={packet.symbol} raw={symbol}")
        symbol = packet.symbol

    action = str(raw.get("action") or "stand_down")
    if action not in VALID_ADVISORY_ACTIONS:
        warnings.append(f"invalid action {action!r}; coerced to stand_down")
        action = "stand_down"

    playbook_raw = raw.get("playbook")
    playbook = str(playbook_raw) if playbook_raw else None
    if playbook is not None and playbook not in VALID_PLAYBOOKS:
        warnings.append(f"invalid playbook {playbook!r}; dropped")
        playbook = None

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        warnings.append("bad confidence; coerced to 0")
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    missing_evidence = sorted(REQUIRED_EVIDENCE_KEYS - set(evidence))
    if missing_evidence:
        warnings.append(f"missing evidence keys: {missing_evidence}")

    if packet.scope != "v1" and action in {"watch_playbook", "maintain"}:
        warnings.append(f"{packet.symbol} is {packet.scope}; execution-facing advice coerced to paper_only")
        action = "paper_only"

    deterministic_allowed = bool(packet.deterministic_route.get("execution_allowed"))
    if not deterministic_allowed and action in {"watch_playbook", "maintain"}:
        warnings.append("deterministic route is not execution_allowed; coerced to paper_only")
        action = "paper_only"

    if action == "watch_playbook" and playbook is None:
        warnings.append("watch_playbook requires a valid playbook; coerced to maintain")
        action = "maintain"

    allowed_for_execution = (
        packet.scope == "v1"
        and deterministic_allowed
        and action in {"maintain", "watch_playbook"}
        and not missing_evidence
        and confidence >= 0.70
    )

    # AI can never force execution; it only marks advice as eligible for a
    # deterministic layer to consider.
    if raw.get("execute") is True or raw.get("place_order") is True:
        warnings.append("execution request ignored; AI advisory cannot place orders")
        allowed_for_execution = False

    rationale = str(raw.get("rationale") or "")
    if len(rationale) > 800:
        warnings.append("rationale truncated to 800 chars")
        rationale = rationale[:800]

    return AdvisoryDecision(
        decision_ts=_utc_now(),
        symbol=packet.symbol,
        action=action,
        playbook=playbook,
        confidence=round(confidence, 4),
        rationale=rationale,
        evidence=evidence,
        allowed_for_execution=allowed_for_execution,
        warnings=tuple(warnings),
    )
