"""A notification must never be able to break trading."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import notify


def test_send_never_raises_when_unconfigured(monkeypatch):
    monkeypatch.setattr(notify, "_loaded", True)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert notify.send("anything") is False


def test_send_never_raises_on_network_failure(monkeypatch):
    monkeypatch.setattr(notify, "_loaded", True)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    assert notify.send("anything") is False        # swallowed, not raised


def test_a_rejection_is_announced_not_just_fills(monkeypatch):
    """Every order attempted so far has been a rejection; a fills-only feed
    would have stayed silent through all five."""
    seen = {}
    monkeypatch.setattr(notify, "send", lambda text, title=None: seen.update(
        {"text": text, "title": title}) or True)
    notify.order_event("swing", "ZEC", "long",
                       {"filled": False, "status": "rejected_v1_allowlist",
                        "error": "ZEC is not in v1 allowlist"})
    assert "REJECTED" in seen["title"] and "ZEC" in seen["title"]
    assert "allowlist" in seen["text"]


def test_a_fill_reports_the_levels(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify, "send", lambda text, title=None: seen.update(
        {"text": text, "title": title}) or True)
    notify.order_event("fade", "HYPE", "long",
                       {"filled": True, "size_coin": 3.2, "entry_oid": 99},
                       {"entry_px": 81.87, "sl_px": 78.75, "tp_px": 86.09})
    assert "FILL" in seen["title"]
    for v in ("81.87", "78.75", "86.09", "99"):
        assert v in seen["text"]


def test_explicit_user_agent_is_set():
    """Discord's Cloudflare 403s urllib's default UA -- the kind of thing that
    fails once, silently, at 3am."""
    assert "HyphyLiquid" in notify._UA
