"""Attestations are decisions someone made; they must outlive one invocation.

Before this was stored, --attest applied only to the run that set it, so a lane
printed FORWARD_PAPER once and silently fell back to RESEARCH_ONLY on the next
bare run. That makes the ladder meaningless.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "graduation_scorecard.py"
STORE = PROJECT_ROOT / "data" / "attestations.json"


def _load():
    if not STORE.exists():
        pytest.skip("no attestation store yet")
    return json.loads(STORE.read_text())


def test_store_is_valid_json_with_provenance():
    store = _load()
    for key, fields in store.items():
        assert ":" in key, f"key {key!r} must be SYMBOL:LANE"
        for field, meta in fields.items():
            assert meta.get("by"), f"{key}/{field} has no attester"
            assert meta.get("at"), f"{key}/{field} has no timestamp"


def test_attested_lanes_survive_a_bare_rerun():
    """The exact regression: run with no flags, attestations still apply."""
    store = _load()
    if not store:
        pytest.skip("nothing attested")
    r = subprocess.run([sys.executable, str(SCRIPT)], cwd=PROJECT_ROOT,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    for key, fields in store.items():
        if "logic_makes_market_sense" not in fields:
            continue
        sym = key.split(":")[0]
        block = [b for b in r.stdout.split("\n\n") if b.startswith(f"{sym} /")]
        assert block, f"{sym} missing from scorecard"
        assert "logic_makes_market_sense not attested" not in block[0], (
            f"{sym} lost its stored attestation on a bare run")


def test_unknown_field_is_rejected():
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--attest", "SOL:funding_neg_fade:not_a_real_gate"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 2
    assert "unknown attestation field" in r.stderr


def test_malformed_spec_is_rejected():
    r = subprocess.run([sys.executable, str(SCRIPT), "--attest", "garbage"],
                       cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 2
