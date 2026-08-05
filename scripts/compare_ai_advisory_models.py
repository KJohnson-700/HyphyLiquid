"""Compare advisory decisions from different AI models.

This is for Sonnet-vs-Opus style bake-offs. It scores advisory outputs on
guardrail cleanliness and usefulness, not on PnL. PnL attribution comes later
after the deterministic paper/live path has enough matched outcomes.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_DECISIONS = DATA_DIR / "ai_advisory_decisions.jsonl"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _decision_score(row: dict) -> float:
    """Score a model advisory row for operating usefulness."""
    score = 0.0
    warnings = row.get("warnings", [])
    evidence = row.get("evidence", {}) if isinstance(row.get("evidence"), dict) else {}
    action = row.get("action")
    if not warnings:
        score += 2.0
    else:
        score -= min(2.0, len(warnings) * 0.5)
    score += min(3.0, len(set(evidence) & {"regime", "tape", "risk"}) * 1.0)
    if action in {"stand_down", "maintain", "paper_only", "watch_playbook"}:
        score += 1.0
    if row.get("allowed_for_execution"):
        score += 1.0
    rationale = str(row.get("rationale") or "")
    if 80 <= len(rationale) <= 800:
        score += 1.0
    confidence = float(row.get("confidence") or 0.0)
    if 0.2 <= confidence <= 0.95:
        score += 0.5
    return round(score, 4)


def build_comparison(rows: list[dict]) -> dict:
    """Build aggregate comparison stats by model_id."""
    by_model: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_model[str(row.get("model_id") or "unknown")].append(row)

    models = {}
    for model, model_rows in sorted(by_model.items()):
        scores = [_decision_score(row) for row in model_rows]
        warnings = sum(len(row.get("warnings", [])) for row in model_rows)
        eligible = sum(1 for row in model_rows if row.get("allowed_for_execution"))
        actions = defaultdict(int)
        for row in model_rows:
            actions[str(row.get("action"))] += 1
        models[model] = {
            "n": len(model_rows),
            "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
            "warnings": warnings,
            "execution_eligible": eligible,
            "actions": dict(sorted(actions.items())),
        }
    ranked = sorted(models.items(), key=lambda item: (-item[1]["avg_score"], item[1]["warnings"], item[0]))
    return {"models": models, "ranking": [{"model_id": model, **stats} for model, stats in ranked]}


def render_markdown(report: dict) -> str:
    lines = [
        "# AI Advisory Model Comparison",
        "",
        "This compares advisory quality only. It does not claim trading edge until matched paper/live outcomes exist.",
        "",
        "| Rank | Model | n | Avg score | Warnings | Execution-eligible | Actions |",
        "|---:|---|---:|---:|---:|---:|---|",
    ]
    for idx, row in enumerate(report["ranking"], start=1):
        actions = ", ".join(f"{k}:{v}" for k, v in row["actions"].items())
        lines.append(
            f"| {idx} | {row['model_id']} | {row['n']} | {row['avg_score']} | "
            f"{row['warnings']} | {row['execution_eligible']} | {actions} |"
        )
    if not report["ranking"]:
        lines.append("|  | none | 0 | 0 | 0 | 0 |  |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    parser.add_argument("--out", type=Path, default=DATA_DIR / "ai_advisory_model_comparison.md")
    args = parser.parse_args()
    rows = _load_jsonl(args.decisions)
    report = build_comparison(rows)
    args.out.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"rows": len(rows), "models": len(report["models"]), "out": str(args.out)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
