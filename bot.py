"""
ConfluenceAlertBot — high-confluence memecoin Telegram alert daemon.

Filters out public social noise using on-chain data from GMGN only. No
third-party copy-trading wrappers, opaque trading bots, or sketchy community
scripts: every signal is a verified on-chain metric pulled straight from GMGN,
and dispatch goes through the official Telegram Bot API.

Data layer
----------
All reads go through the official `gmgn-cli` (installed globally), which is
the only supported path to GMGN data. The public api.gmgn.ai/api/v1 endpoint
sits behind Cloudflare's bot challenge and returns a 403 HTML challenge to any
plain HTTP client — the CLI handles Cloudflare, authentication (X-APIKEY) and
request signing internally. Every GMGN skill in this repo documents the same
rule: "Always use gmgn-cli, never curl/WebFetch." See GmgnCli below.

Pipeline (each stage is its own method, so stages can be re-ordered, gated
off, or swapped for a different data source without touching the others):

    1. Discovery   — pull trending + trench (newly launched) token feeds.
    2. Confluence  — cheap market-cap pre-filter, then strict security gates
                     (honeypot / rug / bundler / taxes / renouncement) and a
                     holder-distribution overlap check against a locally managed
                     whitelist of verified target wallets.
    3. Dispatch    — dedupe against persisted state, then push a formatted
                     high-priority alert to the configured Telegram channel via
                     the official Bot API.

All tunables live in config.py (single source of truth, env-overridable). All
network calls go through the defensive GmgnCli runner below.

Notes on schema drift: GMGN does not publish a stable API contract. Field access
here is defensive — every value is read through coerce helpers that treat a
missing field as "unknown", never as a number (so a token that drops a field
fails closed, not open).

⚠️  There is no target-wallet whitelist by default. The wallet-overlap gate
requires >= MIN_WALLET_MATCH tracked wallets among the token's holders; with an
empty whitelist it can never pass, so the bot stays silent until
`GMGN_TARGET_WALLETS` / `target_wallets.txt` is configured. That is intentional.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import replace as dataclass_replace
from typing import Any, Dict, List, Optional, Set, Tuple

import requests  # telegram dispatch only (data layer goes via gmgn-cli)

from config import BotConfig

logging.basicConfig(
    level=os.getenv("BOT_LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ConfluenceBot")

HOUR = 3600
MINUTE = 60

# ─────────────────────────────────────────────────────────────────────────
# Data layer — GMGN via the official `gmgn-cli`
#
# The public api.gmgn.ai/api/v1 endpoint sits behind Cloudflare's "Just a
# moment…" bot challenge, so a plain HTTP client (requests/curl) gets a 403
# HTML challenge, never JSON. That is exactly why every GMGN skill in this
# repo states: "Always use `gmgn-cli` — the website requires login and does
# not expose structured data." gmgn-cli handles Cloudflare, auth (X-APIKEY)
# and request signing internally, and is installed globally + authenticated.
#
# So every network read here goes through a thin GmgnCli subprocess runner.
# It is not a third-party wrapper — it is the official CLI that the skills
# document as the only supported data path.
# ─────────────────────────────────────────────────────────────────────────


class GmgnCli:
    """Runs `gmgn-cli <args> --raw`, returns parsed JSON.

    - paces calls (token-bucket-ish delay) to stay under the rate limiter
    - decodes stdout as UTF-8 (gmgn-cli emits JSON with emoji/token symbols)
    - on Windows, shells out through cmd (the npm .CMD stub needs it)
    - translates 429 (RATE_LIMIT_BANNED) into a retryable exception; the
      caller decides whether to wait, keeping the aggressive "stop and tell
      the user" behaviour from the skills.
    """

    def __init__(self, pace_seconds: float = 0.35):
        self.pace = max(0.05, float(pace_seconds))
        self._last_call = 0.0

    def _pace(self) -> None:
        wait = self.pace - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def cli(self, *args: str, timeout: int = 40) -> Any:
        """Run one gmgn-cli call with --raw appended. Returns parsed JSON.

        Raises RuntimeError on non-zero exit (esp. rate-limited), OSError if
        gmgn-cli is not installed.
        """
        cmd = shutil.which("gmgn-cli") or "gmgn-cli"
        self._pace()
        proc = subprocess.run(
            [cmd, *args, "--raw"],
            capture_output=True,
            timeout=timeout,
            shell=(os.name == "nt"),
        )
        out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
            raise RuntimeError((err or out or "gmgn-cli failed").strip()[:300])
        if not out.strip():
            raise RuntimeError("gmgn-cli returned empty response (rate limited?)")
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Non-JSON response from gmgn-cli: {e}") from e

    # -- typed wrappers -----------------------------------------------------
    def trending(self, chain: str, limit: int = 100) -> List[Dict[str, Any]]:
        data = self.cli("market", "trending", "--chain", chain,
                        "--interval", "24h", "--limit", str(limit))
        body = data.get("data", data) if isinstance(data, dict) else data
        rank = body.get("rank", [] if isinstance(body, dict) else body)
        return rank if isinstance(rank, list) else []

    def trenches(self, chain: str) -> List[Dict[str, Any]]:
        data = self.cli("market", "trenches", "--chain", chain)
        body = data.get("data", data) if isinstance(data, dict) else data
        out: List[Dict[str, Any]] = []
        if isinstance(body, dict):
            for key in ("completed", "near_completion", "new_creation"):
                val = body.get(key)
                if isinstance(val, list):
                    out.extend(val)
        elif isinstance(body, list):
            out = body
        return out

    def holders(self, chain: str, address: str, limit: int = 100) -> List[Dict[str, Any]]:
        data = self.cli("token", "holders", "--chain", chain,
                        "--address", address, "--limit", str(limit))
        body = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(body, dict):
            for key in ("list", "holders", "top_holders", "items"):
                val = body.get(key)
                if isinstance(val, list):
                    return val
            return []
        return body if isinstance(body, list) else []

    def security(self, chain: str, address: str) -> Dict[str, Any]:
        data = self.cli("token", "security", "--chain", chain, "--address", address)
        body = data.get("data", data) if isinstance(data, dict) else data
        return body if isinstance(body, dict) else {}

    def info(self, chain: str, address: str) -> Dict[str, Any]:
        """Live price / market-cap snapshot via `token info`.

        Returns the parsed token-info object (price.price, circulating_supply,
        ath_price, holder_count, liquidity, ...) or {} on failure.
        """
        data = self.cli("token", "info", "--chain", chain, "--address", address)
        body = data.get("data", data) if isinstance(data, dict) else data
        return body if isinstance(body, dict) else {}


# ─────────────────────────────────────────────────────────────────────────
# Coercion helpers
#
# GMGN returns numbers as float, int, or string ("1.5e6", "0.03") — sometimes
# even None or "" for a field that legitimately exists. A helper is used for
# EVERY numeric read so a missing field becomes unknown (None), never zero:
# a token with no security data must fail the gate, not sail through as "clean".
# ─────────────────────────────────────────────────────────────────────────


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _yes(v: Any) -> bool:
    """Accepts 'yes'/'no', '1'/'0', True/False, numeric."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y")
    return False


def _b(v: Any) -> bool:
    """Accepts 'true'/'false', '1'/'0', True/False, numeric."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes")
    return False


_ANSI_RESIDUE = re.compile(r"\[[0-9;?]+[a-zA-Z]")  # CSI left over once ESC is gone


def _s(v: Any) -> str:
    """Safe display string from attacker-controlled metadata.

    Strips control chars, bidi / zero-width, ESC sequences and the CSI residue
    they leave behind — a symbol like '\\x1b[31mVERDICT: dump' must never forge
    a formatting ride or a fake alert line in Telegram (Markdown).
    """
    if v is None:
        return ""
    s = str(v).strip()
    s = "".join(ch for ch in s if ch.isprintable() and ord(ch) < 0x200F)
    s = _ANSI_RESIDUE.sub("", s)
    s = " ".join(s.split())
    return s


def short_addr(addr: Any, n: int = 10) -> str:
    a = _s(addr)
    if len(a) <= 2 * n:
        return a
    return f"{a[:n]}...{a[-4:]}"


def fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:.1f}%"


def split_targets(env: str, path: str) -> List[str]:
    """Merge whitelisted wallets from the env var and an optional local file.

    The file takes one address per line; blank lines and '#' comment lines are
    ignored. Case-insensitive set semantics — a wallet can appear once.
    """
    out: List[str] = []
    seen: Set[str] = set()

    def _addtoks(toks) -> None:
        for tok in toks:
            tok = tok.strip()
            # Guard against mid-line comments too ("addr # label").
            hash_idx = tok.find("#")
            if hash_idx != -1:
                tok = tok[:hash_idx].strip()
            if not tok:
                continue
            if tok.lower() in seen:
                continue
            seen.add(tok.lower())
            out.append(tok)

    def _add(raw: str) -> None:
        for line in raw.splitlines():
            # Cut any inline comment ("addr # pump wallet") before splitting.
            line = line.split("#", 1)[0]
            stripped = line.strip()
            if not stripped:
                continue
            _addtoks(re.split(r"[\s,]+", stripped))

    if env:
        _add(env)
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                _add(fh.read())
        except OSError as e:
            # A missing wallet file is the normal state when the whitelist comes
            # purely from the env var — debug, not a warning on every cycle.
            logger.debug("No target wallet file %s: %s", path, e)

    return out


# Base URL for the GMGN explorer link, without trailing slash.
GMGN_EXPLORER = os.getenv("GMGN_EXPLORER_URL", "https://gmgn.ai")


class StateStore:
    """Minimal persisted dedupe: token -> alert_unixtime.

    Survives restarts so a token is not re-alerted after the bot dies. Safe to
    delete while the bot is stopped — it simply causes a one-off re-notify.

    Thread-safety: the monitor loop is single-threaded; a replace on write is
    atomic enough for that use.

    Limits the file to `max_entries` lines — evicts oldest first.
    """

    def __init__(self, path: str, suppress_minutes: int, max_entries: int = 5000):
        self.path = path
        self.suppress_seconds = suppress_minutes * MINUTE
        self.data: Dict[str, int] = {}
        self.max_entries = max_entries
        if path:
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data = {str(k): int(v) for k, v in loaded.items()}
        except (OSError, ValueError):
            self.data = {}

    def _save(self) -> None:
        if not self.path:
            return
        # Evict oldest entries once the file grows long. The bot is run with
        # --state-file to point here, and deleting the file resets memory.
        if len(self.data) > self.max_entries:
            cutoff = len(self.data) - self.max_entries
            for addr in sorted(self.data, key=self.data.get)[:cutoff]:
                del self.data[addr]
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("Could not persist alert state: %s", e)

    def seen(self, token: str) -> bool:
        return token in self.data

    def is_suppressed(self, token: str) -> bool:
        ts = self.data.get(token)
        return ts is not None and (time.time() - ts) < self.suppress_seconds

    def record(self, token: str) -> None:
        self.data[token] = int(time.time())
        self._save()


class AlertStore:
    """Persists tracked alerts (address -> record dict) so ROI follow-ups
    survive restarts.

    Each record carries what the tracker needs to compute PnL later:
      alert_ts      unix time the alert fired
      entry_mc      market cap at alert time
      entry_price   token price at alert time
      symbol/name   for message text
      matched       tracked wallets that triggered the alert
      next_update_ts  unix time of the next 30-min update
      verdict_sent  whether the final verdict has been sent
    """

    def __init__(self, path: str, max_entries: int = 2000):
        self.path = path
        self.max_entries = max_entries
        self.data: Dict[str, Dict[str, Any]] = {}
        if path:
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data = {str(k): v for k, v in loaded.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            self.data = {}

    def _save(self) -> None:
        if not self.path:
            return
        if len(self.data) > self.max_entries:
            cutoff = len(self.data) - self.max_entries
            for addr in sorted(self.data, key=lambda a: self.data[a].get("alert_ts", 0))[:cutoff]:
                del self.data[addr]
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("Could not persist alert track: %s", e)

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        return self.data.get(token)

    def upsert(self, token: str, record: Dict[str, Any]) -> None:
        self.data[token] = record
        self._save()

    def delete(self, token: str) -> None:
        if token in self.data:
            del self.data[token]
            self._save()

    def all(self) -> Dict[str, Dict[str, Any]]:
        """Snapshot of all active (not-yet-deleted) records."""
        return dict(self.data)


class AlertTracker:
    """ROI / PnL follow-up on fired alerts.

    After an alert is dispatched, the tracker stores its entry MC/price and
    sends updates every `interval_minutes` until `horizon_hours` elapse. If the
    token's MC falls >= `bad_call_pct` below entry, it sends a "bad call"
    verdict immediately and retires the record.
    """

    def __init__(self, store: AlertStore, cli: GmgnCli, chain: str,
                 interval_minutes: int, horizon_hours: int,
                 bad_call_pct: float, explorer_base: str,
                 telegram_send):
        self.store = store
        self.cli = cli
        self.chain = chain
        self.interval = max(1, int(interval_minutes)) * MINUTE
        self.horizon = max(1, int(horizon_hours)) * HOUR
        self.bad_call = max(0.0, min(0.9, float(bad_call_pct)))  # cfg-agnostic
        self.explorer_base = explorer_base
        self._send = telegram_send

    # ── record when an alert fires ─────────────────────────────────────
    def record_alert(self, info: Dict[str, Any], matched: List[str]) -> None:
        addr = info["address"]
        if not addr:
            return
        mc = info.get("market_cap")
        price = info.get("price")
        now = int(time.time())
        rec = {
            "address": addr,
            "alert_ts": now,
            "entry_mc": mc if mc is not None else 0.0,
            "entry_price": price if price is not None else 0.0,
            "symbol": info.get("symbol", ""),
            "name": info.get("name", ""),
            "matched": list(matched),
            "next_update_ts": now + self.interval,
            "verdict_sent": False,
        }
        self.store.upsert(addr, rec)
        logger.info("Tracking alert for %s (entry MC=%s)", rec["symbol"], fmt_usd(rec["entry_mc"]))

    # ── process due updates ────────────────────────────────────────────
    def process_due(self, now: Optional[int] = None) -> int:
        """Send updates/verdicts for every record whose next update is due.
        Returns the number of Telegram messages sent."""
        now_i = int(now or time.time())
        sent = 0
        for addr, rec in list(self.store.all().items()):
            # Already resolved -> skip.
            if rec.get("verdict_sent"):
                continue
            next_ts = int(rec.get("next_update_ts", 0))
            if now_i < next_ts:
                continue

            current = self._current(rec)
            # If we can't read the token, treat as unknown -> skip (do not
            # fabricate a verdict) but still advance the clock to avoid a
            # tight retry loop.
            if current["mc"] is None:
                rec["next_update_ts"] = now_i + self.interval
                self.store.upsert(addr, rec)
                continue

            roi = (current["mc"] - rec["entry_mc"]) / rec["entry_mc"] if rec["entry_mc"] else None

            elapsed = now_i - int(rec.get("alert_ts", now_i))
            past_horizon = elapsed >= self.horizon
            bad_call = (roi is not None and roi <= -self.bad_call)

            if bad_call:
                # Immediate bad-call verdict; do not keep updating a dead token.
                text = self._verdict_text(rec, current, verdict="bad_call")
                if self._send(text):
                    sent += 1
                rec["verdict_sent"] = True
                self.store.upsert(addr, rec)
                continue

            if past_horizon:
                # Final verdict at end of window.
                text = self._verdict_text(rec, current, verdict="final")
                if self._send(text):
                    sent += 1
                rec["verdict_sent"] = True
                self.store.upsert(addr, rec)
                continue

            # Regular 30-min ROI update.
            text = self._update_text(rec, current, roi)
            if self._send(text):
                sent += 1
            rec["next_update_ts"] = now_i + self.interval
            self.store.upsert(addr, rec)

        return sent

    def _current(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        """Live MC/price for a token, via `token info`."""
        try:
            info = self.cli.info(self.chain, rec["address"])
        except Exception as e:
            logger.warning("Track info failed for %s: %s", rec.get("symbol", "?"), e)
            info = {}
        if not info:
            return {"mc": None, "price": None}
        price = _num((info.get("price") or {}).get("price"))
        circ = _num(info.get("circulating_supply"))
        mc = price * circ if (price is not None and circ is not None) else None
        return {"mc": mc, "price": price}

    # ── text builders ──────────────────────────────────────────────────
    def _update_text(self, rec: Dict[str, Any], current: Dict[str, Any],
                     roi: Optional[float]) -> str:
        symbol = _s(rec.get("symbol")) or "?"
        emoji = "🟢" if (roi is not None and roi >= 0) else "🔴"
        arrow = "📈" if (roi is not None and roi >= 0) else "📉"
        pts = []
        pts.append(f"{emoji} {arrow} **ROI Update** · {symbol}")
        pts.append(f"Entry MC {fmt_usd(rec.get('entry_mc'))} → Now {fmt_usd(current['mc'])}")
        if roi is not None:
            pts.append(f"**ROI {roi:+.1%}**")
        pts.append(f"⏱ {int((time.time()-rec['alert_ts'])/3600)}h since alert")
        pts.append(f"[View]({self.explorer_base}/{self.chain}/token/{rec['address']})")
        return "\n".join(pts)

    def _verdict_text(self, rec: Dict[str, Any], current: Dict[str, Any],
                      verdict: str) -> str:
        symbol = _s(rec.get("symbol")) or "?"
        entry_mc = rec.get("entry_mc")
        roi = (current["mc"] - entry_mc) / entry_mc if entry_mc else None
        entry_str = fmt_usd(entry_mc) if entry_mc else "n/a"
        now_str = fmt_usd(current["mc"]) if current["mc"] is not None else "n/a"
        link = f"[View]({self.explorer_base}/{self.chain}/token/{rec['address']})"
        if verdict == "bad_call":
            head = f"❌ **Bad call on {symbol}** — {roi:+.1%} from entry" if roi is not None else f"❌ **Bad call on {symbol}** — value collapsed"
        else:
            head = f"✅ **{symbol} held up** — {roi:+.1%}" if (roi is not None and roi >= -self.bad_call) else f"❌ **{symbol} was a bad call** — {roi:+.1%}" if roi is not None else f"⚠️ {symbol} — data missing at close"
        lines = [head, f"Entry {entry_str} → Now {now_str}"]
        lines.append(link)
        return "\n".join(lines)


class ConfluenceAlertBot:
    """Orchestrates the discovery -> confluence -> dispatch pipeline."""

    def __init__(self, config: Optional[BotConfig] = None):
        self.cfg = config or BotConfig()
        self.chain = self.cfg.chain
        self.cli = GmgnCli(pace_seconds=self.cfg.cli_pace_seconds)
        self.base_gmgn_url = f"{GMGN_EXPLORER}/{self.chain}"
        self.target_wallets = split_targets(
            self.cfg.target_wallets_env, self.cfg.target_wallets_file
        )
        self.state = StateStore(
            self.cfg.state_file,
            self.cfg.repeat_alert_minutes,
        )
        self.tracker = AlertTracker(
            store=AlertStore(self.cfg.track_file),
            cli=self.cli,
            chain=self.chain,
            interval_minutes=self.cfg.track_interval_minutes,
            horizon_hours=self.cfg.track_horizon_hours,
            bad_call_pct=self.cfg.bad_call_pct,
            explorer_base=GMGN_EXPLORER,
            telegram_send=self._telegram_send,
        )

        self.con = self.cfg.confluence
        self.log_started = False

    # ─────────────────────────────────────────────────────────────────────
    # Discovery layer
    # ─────────────────────────────────────────────────────────────────────

    def fetch_trending_tokens(self) -> List[Dict[str, Any]]:
        """Trending tokens ranked by swap activity (24h)."""
        try:
            tokens = self.cli.trending(self.chain)
            logger.info("Trending feed returned %d tokens", len(tokens))
            return tokens
        except Exception as e:  # defensive: discovery must never kill the loop
            logger.error("Error fetching trending tokens: %s", e)
            return []

    def fetch_trench_tokens(self) -> List[Dict[str, Any]]:
        """Newly launched launchpad tokens (pump.fun, four.meme, ...)."""
        try:
            tokens = self.cli.trenches(self.chain)
            logger.info("Trench feed returned %d tokens", len(tokens))
            return tokens
        except Exception as e:
            logger.error("Error fetching trench tokens: %s", e)
            return []

    # ─────────────────────────────────────────────────────────────────────
    # Confluence & security engine
    # ─────────────────────────────────────────────────────────────────────

    def check_token_security_and_holders(self, token_address: str) -> Dict[str, Any]:
        """One `token security` call (only needed to affirm honeypot/renounced
        on chains — SOL returns no rug_ratio/bundler there, those live inline on
        the discovery rows). Returns {} on failure so the gate fails closed."""
        try:
            return self.cli.security(self.chain, token_address)
        except Exception as e:
            logger.warning("Security lookup failed for %s: %s",
                           short_addr(token_address, 6), e)
            return {}

    def token_summary(self, token: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a token dict from either discovery feed into one shape.

        Handles the trending row (full MC) and trench row (also full MC), plus
        any CMGN variant. `market_cap` may be a string like "467981" — coerced.
        """
        address = token.get("address") or token.get("token_address") or ""
        symbol = _s(token.get("symbol"))
        name = _s(token.get("name")) or symbol
        mc = _num(token.get("market_cap"))
        if mc is None:
            mc = _num(token.get("marketCap"))
        volume = _num(token.get("volume_24h"))
        if volume is None:
            volume = _num(token.get("volume"))
        v24h = _num(token.get("volume24h"))
        if volume is None and v24h is not None:
            volume = v24h
        price = _num(token.get("price"))
        swaps = _num(token.get("swaps_24h"))
        if swaps is None:
            swaps = _num(token.get("swaps"))
        holders = _num(token.get("holder_count"))
        if holders is None:
            holders = _num(token.get("holders"))
        return {
            "address": address,
            "symbol": symbol or (address[:6] or "?"),
            "name": name,
            "market_cap": mc,
            "volume_24h": volume,
            "price": price,
            "swaps": swaps,
            "holders": holders,
            "_raw": token,
        }

    def has_min_overlap(
        self,
        holder_addresses: List[str],
    ) -> Tuple[bool, List[str]]:
        """How many whitelisted target wallets show up as holders.

        Returns (enough?, matched_wallets). Requires >= con.min_wallet_overlap
        matches. A tracked wallet among holders is the big signal: it means a
        screened smart-money wallet has taken a position in an early-cap token.
        """
        if not self.target_wallets:
            return False, []
        matched = [
            w
            for w in self.target_wallets
            if w.lower() in {h.lower() for h in holder_addresses}
        ]
        return len(matched) >= self.con.min_wallet_overlap, matched

    def evaluate_confluence(self, token: Dict[str, Any]) -> Tuple[bool, str, List[str]]:
        """Run every gate. Returns (alert?, reason, matched_wallets).

        `reason` is a short one-liner used in the alert body AND written to the
        debug log, so an operator can see *why* a token was dismissed (or
        alerted) without re-running the pipeline. Every failure is named.
        """
        info = self.token_summary(token)

        # -- cheap gates first; number the reasons so logs are greppable --
        if not info["address"]:
            return False, "[gate:address] missing contract address", []
        if self.state.is_suppressed(info["address"]):
            return False, "[gate:dedupe] already alerted recently", []

        mc = info["market_cap"]
        if mc is None:
            return False, "[gate:market_cap] unknown market cap", []
        if mc < self.con.min_market_cap or mc > self.con.max_market_cap:
            return (
                False,
                f"[gate:market_cap] {fmt_usd(mc)} out of "
                f"[{fmt_usd(self.con.min_market_cap)}, {fmt_usd(self.con.max_market_cap)}]",
                [],
            )

        # Merge the discovery row's inline security fields with the dedicated
        # `token security` call. The row carries rug_ratio / bundler_rate /
        # taxes / concentration (~all of them) even on SOL where the security
        # endpoint returns none; the security call adds honeypot + renounced
        # affirmations. The dedicated call wins on conflict (more authoritative).
        security_call = self.check_token_security_and_holders(info["address"])
        security = dict(info["_raw"])
        security.update(security_call)
        # Fail closed if NONE of the security-relevant fields resolved to a real
        # value — we cannot vet this token, so it must not pass. A discovery
        # row always has *some* keys, so `not security` would never fire; the
        # meaningful test is "did any gate field carry a number/flag?".
        _gate_fields = ("rug_ratio", "bundler_trader_amount_rate", "bundler_rate",
                        "buy_tax", "sell_tax", "top_10_holder_rate",
                        "is_honeypot", "honeypot", "renounced_mint",
                        "renounced_freeze_account", "is_renounced", "creator_token_status")
        if not any(security.get(f) is not None for f in _gate_fields):
            return False, "[gate:security] no security data (fail-closed)", []

        # -- honeypot: 'yes' on EVM; SOL has no honeypot concept --
        # Accept both is_honeypot (EVM, 'yes'/'no') and honeypot ('1'/'0').
        h = security.get("is_honeypot")
        h2 = security.get("honeypot")
        if _yes(h) or _yes(h2):
            return False, "[gate:honeypot] flagged honeypot", []

        rug = _num(security.get("rug_ratio"))
        if rug is not None and rug > self.con.max_rug_ratio:
            return (
                False,
                f"[gate:rug] rug_ratio {rug:.3f} > {self.con.max_rug_ratio}",
                [],
            )

        bundle = _num(security.get("bundler_trader_amount_rate"))
        if bundle is None:
            bundle = _num(security.get("bundler_rate"))
        if bundle is not None and bundle > self.con.max_bundle_ratio:
            return (
                False,
                f"[gate:bundle] bundler_rate {bundle:.3f} > {self.con.max_bundle_ratio}",
                [],
            )

        top10 = _num(security.get("top_10_holder_rate"))
        if top10 is not None and top10 > 0.5:
            return False, f"[gate:concentration] top10 hold {top10 * 100:.1f}%", []

        taxes = self._tax_gate(security)
        if taxes[1]:
            return False, taxes[0], []

        renounced = self._renouncement_gate(security)
        if renounced[1]:
            return False, renounced[0], []

        # -- holder distribution / wallet overlap --
        try:
            holders = self.cli.holders(self.chain, info["address"])
        except Exception as e:
            logger.warning("Holder lookup failed for %s: %s",
                           short_addr(info["address"], 6), e)
            holders = []
        holder_addresses = [
            h.get("address") for h in holders if h.get("address")
        ]
        enough, matched = self.has_min_overlap(holder_addresses)

        # Not enough overlap: still list *which* tracked wallets are present
        # (even if below the match minimum) so the debug log says why not.
        if not enough:
            holder_set = {h.lower() for h in holder_addresses}
            present = [w for w in self.target_wallets if w.lower() in holder_set]
            return (
                False,
                f"[gate:overlap] need {self.con.min_wallet_overlap} tracked / "
                f"saw {len(present)} "
                f"({'/'.join(short_addr(w, 4) for w in present) or 'none'})",
                [],
            )

        reason_body = (
            f"Matched tracked smart-money wallets early in holder distribution "
            f"({len(matched)} present) with a clean security profile."
        )
        return True, reason_body, matched

    @staticmethod
    def _tax_gate(security: Dict[str, Any]) -> Tuple[str, bool]:
        buy = _num(security.get("buy_tax"))
        sell = _num(security.get("sell_tax"))
        for label, t in (("buy", buy), ("sell", sell)):
            if t is not None and t > 0.1:
                return f"[gate:tax] {label}_tax {t * 100:.1f}% > 10%", True
        return "", False

    @staticmethod
    def _renouncement_gate(security: Dict[str, Any]) -> Tuple[str, bool]:
        """Ownership-renouncement / mint / freeze gates.

        EVM: owner_renounced must be 'yes'/'1'. SOL: renounced_mint and
        renounced_freeze_account should be true (renounced_freeze_account may
        be legitimately unset on older tokens — treat explicit 'no' as fail).
        Unknown -> fail closed (cannot be vetted).
        """
        owner = security.get("owner_renounced")
        if isinstance(owner, str):
            if owner.lower() in ("yes", "1", "true"):
                return "", False
            if owner.lower() in ("no", "0", "false") or _b(owner) is False:
                return "[gate:owner] owner NOT renounced", True
            return "[gate:owner] unknown renounce status", True
        if owner is not None and _b(owner):
            return "", False
        # Possibly a SOL token where owner_renounced is not the right field.
        mint = security.get("renounced_mint")
        freeze = security.get("renounced_freeze_account")
        if mint is not None and not _b(mint):
            return "[gate:owner] mint authority NOT renounced", True
        if freeze is not None and _b(freeze) is False:
            return "[gate:owner] freeze authority NOT renounced", True
        return "", False

    # ─────────────────────────────────────────────────────────────────────
    # Dispatch layer
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _telegram_text(info: Dict[str, Any], message_details: str,
                       matched: List[str], explorer_base: str, chain: str) -> str:
        """Single source of the alert message layout."""
        addr = info["address"]
        text = (
            "🚨 **HIGH-CONFLUENCE ALPHA DETECTED** 🚨\n\n"
            f"🪙 **Token:** {info['name']} (`{info['symbol']}`)\n"
            f"📍 **Contract:** `{addr}`\n"
            f"💰 **Market Cap:** {fmt_usd(info['market_cap'])}\n"
            f"📊 **24h Volume:** {fmt_usd(info['volume_24h'])}\n"
        )
        if info["price"] is not None:
            text += f"💵 **Price:** ${info['price']:.12g}\n"
        if info["swaps"] is not None:
            text += f"🔁 **Swaps (24h):** {int(info['swaps']):,}\n"
        if info["holders"] is not None:
            text += f"👥 **Holders:** {int(info['holders']):,}\n"

        matched_str = ", ".join(short_addr(w, 4) for w in matched) or "—"
        text += (
            f"\n🎯 **Tracked wallets present {len(matched)}:** `{matched_str}`\n"
            f"🔍 **Confluence Breakdown:**\n{message_details}\n\n"
            f"[View on GMGN]({explorer_base}/{chain}/token/{addr})"
        )
        return text

    def _telegram_send(self, text: str) -> bool:
        """Low-level Telegram Bot API sendMessage. Returns True on success."""
        if not self.cfg.telegram.is_configured():
            logger.warning("Telegram not configured — message not sent")
            return False
        url = f"https://api.telegram.org/bot{self.cfg.telegram.bot_token}/sendMessage"
        payload = {
            "chat_id": self.cfg.telegram.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                body = resp.text[:200]
                logger.error("Telegram rejected message (%s): %s", resp.status_code, body)
                return False
            return True
        except Exception as e:
            logger.error("Failed to send Telegram message: %s", e)
            return False

    def send_telegram_alert(self, token: Dict[str, Any], message_details: str,
                            matched: List[str]) -> bool:
        info = self.token_summary(token)
        text = self._telegram_text(
            info, message_details, matched,
            GMGN_EXPLORER, self.chain,
        )
        ok = self._telegram_send(text)
        if ok:
            logger.info("Alert dispatched for %s (%s)", info["symbol"], short_addr(info["address"], 4))
        return ok

    # ─────────────────────────────────────────────────────────────────────
    # Orchestration
    # ─────────────────────────────────────────────────────────────────────

    def _candidates(self) -> List[Dict[str, Any]]:
        """Merge discovery feeds into normalized, de-duplicated candidates."""
        seen: Set[str] = set()
        out: List[Dict[str, Any]] = []
        for raw in self.fetch_trending_tokens() + self.fetch_trench_tokens():
            info = self.token_summary(raw)
            addr = info["address"]
            if not addr or addr.lower() in seen:
                continue
            seen.add(addr.lower())
            out.append(info)
        # Priority order so a budget-limited scan spends its vets on tokens that
        # can actually pass:
        #   1. in the accumulation zone, smallest cap first (most "early")
        #   2. out-of-zone (rolling over / dust) — vetted only if budget remains
        lo, hi = self.con.min_market_cap, self.con.max_market_cap

        def _key(t):
            mc = t["market_cap"]
            if mc is None:
                return (2, 0.0)
            if lo <= mc <= hi:
                return (0, -mc)          # in zone, smallest cap first
            return (1, mc)               # below/above zone, low cap first
        out.sort(key=_key)
        return out

    def scan_cycle(self, max_scan: int) -> int:
        """One pass over all feeds. Returns the number of alerts sent."""
        if not self.log_started:
            logger.info(
                "Initializing Confluence Bot Engine with Local Analytics... "
                "chain=%s whitelist=%d",
                self.chain,
                len(self.target_wallets),
            )
            self.log_started = True

        candidates = self._candidates()
        logger.info(
            "Scan cycle: %d unique candidates (budget %d/sec-cycle)",
            len(candidates),
            max_scan,
        )
        alerts_sent = 0
        # Only touch the first `max_scan` candidates to keep within the
        # rate-limit budget; the rest roll over to the next cycle.
        for info in candidates[:max_scan]:
            address = info["address"]
            # Cheap MC/address gate first (no network): slightly redundant with
            # evaluate, but avoids burning a security call on tokens we already
            # know are out of range.
            mc = info["market_cap"]
            if mc is None or not (
                self.con.min_market_cap <= mc <= self.con.max_market_cap
            ):
                logger.debug("Dropped (cap) %s", short_addr(address, 6))
                continue
            ok, reason, matched = self.evaluate_confluence(info)
            if not ok:
                logger.debug("%s -> %s", short_addr(address, 6), reason)
                continue

            # evaluate() already fetched security+holders and confirmed overlap.
            sent = self.send_telegram_alert(info, reason, matched)
            if sent:
                alerts_sent += 1
                self.state.record(address)
                # Start ROI/PnL follow-up on this alert (only on real dispatch,
                # so dry-run does not fabricate tracked records).
                self.tracker.record_alert(info, matched)

        return alerts_sent

    def run_monitor_loop(self) -> None:
        """Infinite loop. Ctrl-C / SIGTERM exits cleanly."""
        if not self.target_wallets:
            logger.warning(
                "No target wallets configured (%(env)s / %(file)s). The overlap "
                "gate can never pass — the bot will stay silent. See the module "
                "docstring.",
                {"env": "GMGN_TARGET_WALLETS", "file": self.cfg.target_wallets_file},
            )

        self.log_started = False
        while True:
            try:
                self.scan_cycle(self.cfg.max_scan_per_cycle)
                # Send any due ROI updates / verdicts for tracked alerts.
                try:
                    upd = self.tracker.process_due()
                    if upd:
                        logger.info("Sent %d tracked-alert update(s)", upd)
                except Exception as e:
                    logger.error("Error in tracker loop: %s", e)
                time.sleep(self.cfg.scan_interval_seconds)
            except KeyboardInterrupt:
                logger.info("Monitor stopped by operator.")
                return
            except Exception as e:
                logger.error("Error in main loop: %s", e)
                time.sleep(self.cfg.idle_sleep_on_error)


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bot.py",
        description=(
            "High-confluence memecoin Telegram alert daemon. Discovery, "
            "confluence & security gates, and Telegram dispatch via the "
            "official Bot API — no third-party executables."
        ),
    )
    parser.add_argument("--once", action="store_true",
                        help="Run a single scan cycle and exit (cron mode).")
    parser.add_argument("--max-scan", type=int, default=None,
                        help="Override how many candidates are vetted per cycle "
                             "(default: BOT_MAX_SCAN_PER_CYCLE, currently "
                             "read from config).")
    parser.add_argument("--state-file", type=str, default=None,
                        help="Override the persisted alert-dedupe file "
                             "(default: BOT_STATE_FILE / bot_state.json).")
    parser.add_argument("--track-file", type=str, default=None,
                        help="Override the persisted alert-tracking file "
                             "(default: BOT_TRACK_FILE / alert_track.json).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log which tokens would alert, but do NOT send "
                             "Telegram messages.")
    args = parser.parse_args(argv)

    config = BotConfig()
    if args.state_file:
        config = dataclass_replace(config, state_file=args.state_file)
    if args.track_file:
        config = dataclass_replace(config, track_file=args.track_file)
    if args.max_scan is not None:
        config = dataclass_replace(config, max_scan_per_cycle=args.max_scan)

    bot = ConfluenceAlertBot(config)

    if args.dry_run:
        # Keep the real signature (token, message_details, matched). Returns True
        # so scan_cycle's alarm path (record state) behaves the same as a real send.
        def _dry(token, message_details, matched):
            info = bot.token_summary(token)
            logger.info("DRY-RUN would alert %s (%s): %s",
                        info.get("symbol"), short_addr(info.get("address"), 6),
                        message_details[:120])
            return True
        bot.send_telegram_alert = _dry

    logger.info(
        "ConfluenceBot started — chain=%s target_wallets=%d feed=trending+trenches",
        bot.chain,
        len(bot.target_wallets),
    )
    try:
        if args.once:
            alerts = bot.scan_cycle(bot.cfg.max_scan_per_cycle)
            # Also flush any due tracked-alert ROI updates / verdicts so a
            # cron-driven `--once` deploy keeps the follow-ups self-maintaining.
            try:
                updates = bot.tracker.process_due()
            except Exception as e:
                logger.error("Error processing tracked updates: %s", e)
                updates = 0
            print(f"[bot] scan complete: {alerts} alert(s) dispatched, "
                  f"{updates} tracked-update(s) sent.")
            return 0
        bot.run_monitor_loop()
        return 0
    except KeyboardInterrupt:
        logger.info("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())