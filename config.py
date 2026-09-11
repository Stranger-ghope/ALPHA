"""
Centralized configuration — single source of truth for all tunables.

Environment variables are read once at import time. Override any value via
the corresponding env var before importing this module.

DATA LAYER NOTE
---------------
All GMGN reads go through the official `gmgn-cli` (installed globally), not
direct HTTP. The public api.gmgn.ai/api/v1 endpoint sits behind Cloudflare's
bot challenge, so a plain HTTP client gets a 403 challenge page — the CLI (and
every GMGN skill in this repo) is the only supported data path. See bot.py.
"""

import os
from dataclasses import dataclass, field
from typing import Dict


def _load_dotenv_file() -> None:
    """Load a local .env into os.environ (os.environ always wins).

    python-dotenv is used when available because it handles quoting, inline
    comments and multiline values correctly; a tiny fallback parser covers the
    case where it is not installed. Real process env vars take precedence over
    the file so a service-manager override still works.
    """
    path = os.getenv("BOT_ENV_FILE", os.path.join(os.path.dirname(__file__), ".env"))
    if not os.path.exists(path):
        return
    try:
        from dotenv import load_dotenv  # optional dependency
        load_dotenv(path, override=False)
        return
    except ImportError:
        pass
    # Minimal fallback (no quoting semantics, enough for simple KEY=VALUE files).
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                os.environ.setdefault(key, val)
    except OSError:
        pass


_load_dotenv_file()


def _load_gmgn_env() -> Dict[str, str]:
    """Load GMGN credential keys from the shared gmgn-cli config file.

    gmgn-cli reads ~/.config/gmgn/.env (or GMGN_ENV_FILE), and the bot reuses
    that file so it needs no duplicate credential. Values may be:
      - single line:  GMGN_API_KEY=gmgn_xxx
      - quoted multi-line PEM: GMGN_PRIVATE_KEY="-----BEGIN...\n...-----END-----"
    The parser joins a quoted value across continuation lines.
    Returns {key:value} (process env wins downstream).
    """
    out = {}
    path = os.getenv(
        "GMGN_ENV_FILE",
        os.path.join(os.path.expanduser("~"), ".config", "gmgn", ".env"),
    )
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = fh.read()
    except OSError:
        return out  # no shared config: rely on os.environ alone

    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # Only multi-line quoted values are PEM private keys. Treat any other
        # quoted value (e.g. an API key wrapped in quotes) as a simple scalar.
        if val.startswith('"') and val[1:].lstrip().startswith("-----BEGIN"):
            # Quoted multi-line PEM private key: join continuation lines until
            # the closing quote.
            pieces: List[str] = [val[1:]]
            while i < len(lines):
                next_line = lines[i].rstrip()
                i += 1
                if next_line.endswith('"'):
                    pieces.append(next_line[:-1])
                    break
                pieces.append(next_line)
            val = "\n".join(pieces)
        val = val.strip().strip('"').strip("'")
        if key:
            out[key] = val
    return out


_GMGN_SHARED_ENV = _load_gmgn_env()


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass(frozen=True)
class GMGNConfig:
    """GMGN credential sourcing.

    All reads go through `gmgn-cli` (see bot.py) which handles Cloudflare and
    request signing itself, so no HTTP endpoints live here. This config exists
    to document where credentials come from: the shared gmgn-cli env file, with
    process env as an override.
    """
    api_key: str = field(
        default_factory=lambda: (
            os.getenv("GMGN_API_KEY") or _GMGN_SHARED_ENV.get("GMGN_API_KEY", "")
        )
    )
    private_key: str = field(
        default_factory=lambda: (
            os.getenv("GMGN_PRIVATE_KEY") or _GMGN_SHARED_ENV.get("GMGN_PRIVATE_KEY", "")
        )
    )


@dataclass(frozen=True)
class ConfluenceConfig:
    """Hard thresholds for the confluence filter. Override via env vars."""
    min_market_cap: float = field(
        default_factory=lambda: float(os.getenv("CONFLUENCE_MIN_MC", "50000"))
    )
    max_market_cap: float = field(
        default_factory=lambda: float(os.getenv("CONFLUENCE_MAX_MC", "2000000"))
    )
    max_rug_ratio: float = field(
        default_factory=lambda: float(os.getenv("CONFLUENCE_MAX_RUG_RATIO", "0.40"))
    )
    max_bundle_ratio: float = field(
        default_factory=lambda: float(os.getenv("CONFLUENCE_MAX_BUNDLE_RATIO", "0.30"))
    )
    min_unique_holders: int = field(
        default_factory=lambda: int(os.getenv("CONFLUENCE_MIN_HOLDERS", "50"))
    )
    min_wallet_overlap: int = field(
        default_factory=lambda: int(os.getenv("CONFLUENCE_MIN_WALLET_MATCH", "2"))
    )


@dataclass(frozen=True)
class BotConfig:
    chain: str = field(
        default_factory=lambda: os.getenv("BOT_CHAIN", "sol")
    )
    scan_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("BOT_SCAN_INTERVAL", "30"))
    )
    idle_sleep_on_error: int = field(
        default_factory=lambda: int(os.getenv("BOT_ERROR_SLEEP", "10"))
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("BOT_LOG_LEVEL", "INFO")
    )
    # How many unique candidates are run through the expensive security/holder
    # gates per cycle. Guards the rate-limit budget on large trench feeds.
    max_scan_per_cycle: int = field(
        default_factory=lambda: int(os.getenv("BOT_MAX_SCAN_PER_CYCLE", "20"))
    )
    # Local file persisting which tokens have already been alerted, so a
    # restart does not re-notify the same token. Empty = keep in-memory only.
    state_file: str = field(
        default_factory=lambda: os.getenv("BOT_STATE_FILE", "bot_state.json")
    )
    # Dispatched addresses stay suppressed for this many minutes.
    repeat_alert_minutes: int = field(
        default_factory=lambda: int(os.getenv("BOT_ALERT_REPEAT_MINUTES", "1440"))
    )
    # Target-wallet whitelist: either a comma/space/newline separated env var,
    # or a file with one address per line ('#' comments allowed). Both are
    # merged; empty means the overlap gate can never pass — by design.
    target_wallets_env: str = field(
        default_factory=lambda: os.getenv("GMGN_TARGET_WALLETS", "")
    )
    target_wallets_file: str = field(
        default_factory=lambda: os.getenv("GMGN_TARGET_WALLETS_FILE", "target_wallets.txt")
    )
    # Min seconds between gmgn-cli calls. GMGN's leaky-bucket limiter allots
    # 20 requests/sec; a 0.35s pace keeps well under that, so a burst never
    # accumulates into a ban.
    cli_pace_seconds: float = field(
        default_factory=lambda: float(os.getenv("BOT_CLI_PACE_S", "0.35"))
    )

    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    gmgn: GMGNConfig = field(default_factory=GMGNConfig)
    confluence: ConfluenceConfig = field(default_factory=ConfluenceConfig)

    # -- Alert ROI tracking / follow-up -----------------------------------
    # JSON file recording each fired alert so ROI updates + verdicts can be
    # sent on a cadence. Survives restarts; delete the file to reset memory.
    track_file: str = field(
        default_factory=lambda: os.getenv("BOT_TRACK_FILE", "alert_track.json")
    )
    # Minutes between PnL updates per tracked token (30 = twice per hour).
    track_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("BOT_TRACK_INTERVAL_MIN", "30"))
    )
    # Hours to keep tracking a token after its alert was fired.
    # After this window a final verdict is sent and the record is retired.
    track_horizon_hours: int = field(
        default_factory=lambda: int(os.getenv("BOT_TRACK_HORIZON_HOURS", "24"))
    )
    # Drop threshold for a "bad call" verdict. 0.5 = a 50%+ MC drop from
    # entry is a bad call. Set very low (e.g. 0.99) to effectively disable.
    bad_call_pct: float = field(
        default_factory=lambda: float(os.getenv("BOT_BAD_CALL_PCT", "0.50"))
    )
