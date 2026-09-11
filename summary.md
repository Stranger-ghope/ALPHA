# ALPHA — ConfluenceAlertBot

A modular Python Telegram alert daemon that filters out public social noise and
dispatches **high-confluence memecoin alerts** based purely on raw, verified
on-chain data from GMGN. No third-party copy-trading wrappers, no opaque trading
bots, no sketchy community scripts — every signal is a measured on-chain metric,
and dispatch goes through the official Telegram Bot API.

**Repo status:** public (17 Sep 2026). It runs free on GitHub Actions forever
(public repos get unlimited Actions minutes).

---

## What it does

1. **Discovery** — pulls GMGN trending (24h swap-ranked) + trenches (newly
   launched launchpad tokens) feeds.
2. **Confluence & security** — cheap market-cap pre-filter ($50K–$2M accumulation
   zone), then strict gates on every candidate:
   - honeypot, rug_ratio ≤ 0.40, bundler_rate ≤ 0.30
   - buy/sell tax ≤ 10%, owner/mint/freeze renounced
   - top-10 holder concentration ≤ 50%
   - **the confluence signal:** ≥ 2 verified smart-money wallets in the token's
     top-100 holders (the whitelist is the bot's core edge).
3. **Dispatch** — dedupe against persisted state, push a formatted alert to a
   Telegram channel via the official Bot API.
4. **ROI accountability** — every fired alert is tracked:
   - PnL update every 30 minutes vs. the MC at alert time
   - a final verdict after 24 hours (✅ held up / ❌ bad call)
   - an immediate **❌ BAD CALL** flag if the token drops ≥ 50% from entry.

---

## How it's built

| File | Role |
|---|---|
| `bot.py` | The daemon: discovery → confluence → dispatch → ROI tracking. CLI `--once`, `--max-scan`, `--dry-run`, `--state-file`, `--track-file`. |
| `config.py` | Single source of truth for every tunable (env-overridable): MC range, security thresholds, track cadence, credential sourcing. |
| `.github/workflows/bot.yml` | GitHub Actions: run `--once` every 30 min, restore/persist state to a `bot-state` branch, normalize creds. |
| `.env.example` | Template of all config (no real values). |
| `target_wallets.txt` | **Template only** — the real addresses live in the `GMGN_TARGET_WALLETS` secret. |

### Data layer
All reads go through the official **`gmgn-cli`** (installed globally). GMGN's
public `api.gmgn.ai/api/v1` endpoint sits behind Cloudflare's bot challenge,
so a plain HTTP client gets a 403 — the CLI is the only supported path. It
handles Cloudflare, auth (`X-APIKEY`) and request signing internally.

### Credentials (all secrets — never committed)
| Secret | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram dispatch |
| `GMGN_API_KEY` | GMGN exists auth |
| `GMGN_PRIVATE_KEY` | GMGN sign auth (not needed for reads, kept for completeness) |
| `GMGN_TARGET_WALLETS` | The 13 verified smart-money addresses (one per line) |

Local runs read `~/.config/gmgn/.env` (GMGN) and the gitignored
`target_wallets.local.txt` (wallets). CI gets everything from secrets.

---

## Why GitHub Actions (the free deployment)

- **No card, no server, no subscription** — the entire bot is a scheduled CI job.
- **Runs forever**: public repos get **unlimited** Actions minutes.
- **Secrets** keep the bot's signal private even though the code is public.
- A `bot-state` orphan branch persists `bot_state.json` (dedupe) +
  `alert_track.json` (ROI tracking) between runs — so a fresh runner VM each time
  never loses continuity.

### Cadence & budget
Scheduled `:03` / `:33` hourly (every 30 min), 48 runs/day. Each run is ~2.5 min.
On a **private** repo that would exceed the 2,000-min free quota ~2 weeks in —
which is why the repo is **public** (unlimited minutes).

---

## Operations

### Local (this machine)
```bash
python bot.py --once --dry-run       # preview what would alert (no sends)
python bot.py --once                 # real single scan + dispatch
python bot.py                        # long-running daemon
```
Deps: `pip install -r requirements.txt` · `npm install -g gmgn-cli` · keys in
`~/.config/gmgn/.env` + `GMGN_TARGET_WALLETS_FILE` in `.env`.

### CI (deployed)
Runs on schedule automatically. Manual trigger: **Actions → bot → Run workflow**
(inputs `max_scan` default 10, `dry_run`).

### Tunables (env vars, defaults in `config.py`)
`BOT_MAX_SCAN_PER_CYCLE=10` (rate-limit-safe) · `BOT_CLI_PACE_S=1.5` ·
`CONFLUENCE_MIN_MC/MAX_MC=50000/2000000` · `BOT_TRACK_INTERVAL_MIN=30` ·
`BOT_TRACK_HORIZON_HOURS=24` · `BOT_BAD_CALL_PCT=0.50`.

---

## Hard-won lessons (sharp edges)

- **GMGN free-tier IP rate limit bans fast** — a 40-candidate sweep hammering
  ~2 calls each trips `429 RATE_LIMIT_BANNED` and extends the ban if you keep
  going. Solution: `max_scan=10`, 1.5s pacing, and explicit 429 handling that
  parses the server reset time and backs off past it.
- **`gmgn-cli` is IPv4-only** (per the skills). GitHub's runners are IPv4 — fine.
- **Windows + git + craft**: a PEM private key pasted into a GitHub secret with
  CRLF line endings corrupts the key — the workflow strips `\r`. And the
  "Persist state" step must set a git identity before `commit-tree` (the runner
  has none → `empty ident name` → exit 128).
- **Node20 deprecation notices** in Actions logs are non-fatal noise — ignore.

---

## Original prompt / intent
> "Elite quantitative crypto engineer … high-confluence, confidential memecoin
> alerts … entirely through raw, verified on-chain data. Avoid all third-party
> copy-trading wrappers, opaque Telegram trading bots, and sketchy community
> scripts to ensure total security and control over execution logic."