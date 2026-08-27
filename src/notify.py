"""Push notifications for things that happen while nobody is watching.

Two testnet lanes fired five signals over fourteen hours and every one was
refused -- twice by a rounding bug, three times by the allowlist. Nothing
surfaced it, so the failures were found by going and looking rather than by
being told. This is the telling.

Design rules:
  - a notification failure must NEVER break trading. Every path swallows and
    returns False.
  - no-op silently when unconfigured, so nothing depends on it being set up.
  - an explicit User-Agent. Discord's Cloudflare 403s urllib's default, which
    is exactly the sort of thing that fails once, silently, at 3am.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_UA = "HyphyLiquid/1.0 (+bot)"
_loaded = False


def _ensure_env() -> None:
    global _loaded
    if _loaded:
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except Exception:
        pass
    _loaded = True


def send(text: str, *, title: str | None = None) -> bool:
    """Post to Discord. Returns True if delivered. Never raises."""
    _ensure_env()
    url = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    if not url.startswith("https://"):
        return False
    body = {"content": (f"**{title}**\n{text}" if title else text)[:1900]}
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def order_event(lane: str, symbol: str, side: str, result: dict,
                intent: dict | None = None) -> bool:
    """Announce a fill or a rejection.

    Rejections are reported as loudly as fills on purpose: every order this
    project has attempted so far has been a rejection, and a fills-only feed
    would have stayed silent through all of them.
    """
    intent = intent or {}
    if result.get("filled"):
        head = f"FILL — {lane} {symbol} {side.upper()}"
        detail = (f"entry {intent.get('entry_px')}  size {result.get('size_coin')}\n"
                  f"stop {intent.get('sl_px')}  target {intent.get('tp_px')}\n"
                  f"oid {result.get('entry_oid')}")
    else:
        head = f"REJECTED — {lane} {symbol} {side.upper()}"
        detail = (f"status: {result.get('status')}\n"
                  f"{str(result.get('error') or result.get('risk_verdict') or '')[:300]}")
    return send(f"```\n{detail}\n```", title=head)
