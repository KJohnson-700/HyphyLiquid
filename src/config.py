"""
HyphyLiquid — Config loader

Loads settings from config/settings.yaml and env vars from .env.
Falls back to defaults if files don't exist.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

# Resolve paths relative to project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"
ENV_PATH = PROJECT_ROOT / ".env"

ENV_KEYS = [
    "HYPERLIQUID_WALLET_ADDRESS",
    "HYPERLIQUID_PRIVATE_KEY",
    "HYPERLIQUID_ENV",
    "HYPERLIQUID_BANKROLL",
    "HYPERLIQUID_MAX_LEVERAGE",
    "HYPERLIQUID_MAX_RISK_PCT",
    "HYPERLIQUID_DAILY_LOSS_LIMIT_PCT",
    "DISCORD_WEBHOOK_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings() -> Dict[str, Any]:
    """Load settings from config/settings.yaml. Returns empty dict if not found."""
    return _load_yaml(CONFIG_PATH)


def load_env() -> Dict[str, str]:
    """Load env vars from .env. Returns dict of relevant vars that are set."""
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=False)
    return {k: os.getenv(k, "") for k in ENV_KEYS if os.getenv(k)}


def get(key: str, default: Any = None) -> Any:
    """Look up a setting, with env var override."""
    env = load_env()
    if key in env and env[key]:
        return env[key]
    settings = load_settings()
    return settings.get(key, default)


def get_risk_config() -> Dict[str, Any]:
    """Build a dict suitable for RiskConfig(**) from settings + env."""
    settings = load_settings()
    env = load_env()

    def env_or(k_env: str, k_settings: str, default: Any) -> Any:
        if k_env in env and env[k_env] != "":
            return env[k_env]
        if k_settings in settings and settings[k_settings] is not None:
            return settings[k_settings]
        return default

    return {
        "bankroll_usd": float(env_or("HYPERLIQUID_BANKROLL", "bankroll_usd", 1000)),
        "max_risk_per_trade_pct": float(env_or("HYPERLIQUID_MAX_RISK_PCT", "max_risk_per_trade_pct", 0.01)),
        "max_leverage": float(env_or("HYPERLIQUID_MAX_LEVERAGE", "max_leverage", 10)),
        "max_open_positions": int(settings.get("max_open_positions", 3)),
        "daily_loss_limit_pct": float(env_or("HYPERLIQUID_DAILY_LOSS_LIMIT_PCT", "daily_loss_limit_pct", 0.03)),
        "weekly_loss_limit_pct": float(settings.get("weekly_loss_limit_pct", 0.05)),
        "consecutive_loss_halt": int(settings.get("consecutive_loss_halt", 3)),
        "drawdown_kill_pct": float(settings.get("drawdown_kill_pct", 0.40)),
    }


def get_strategy_config() -> Dict[str, Any]:
    """Return the strategy config block, or empty dict if not present."""
    settings = load_settings()
    return settings.get("strategy", {})


def get_logging_config() -> Dict[str, Any]:
    """Return the logging config block, with defaults."""
    settings = load_settings()
    return {
        "level": settings.get("logging", {}).get("level", "INFO"),
        "file": settings.get("logging", {}).get("file", "logs/hyphyliquid.log"),
        "max_bytes": int(settings.get("logging", {}).get("max_bytes", 10_485_760)),
        "backup_count": int(settings.get("logging", {}).get("backup_count", 5)),
    }


def get_env_name() -> str:
    """Return 'testnet' or 'mainnet' based on env var, defaulting to 'testnet'."""
    env = load_env()
    name = env.get("HYPERLIQUID_ENV", "testnet").lower()
    if name not in ("testnet", "mainnet"):
        raise ValueError(f"HYPERLIQUID_ENV must be 'testnet' or 'mainnet', got {name!r}")
    return name
