# ConfluenceAlertBot — GitHub Actions deployment

This is the free, no-card deployment option. GitHub Actions runs the bot's
`--once` mode on a schedule so it doesn't need a server you pay for.

## What you need to do (once)

1. **Set up the GitHub secrets.** The bot needs real credentials, and they
   must live in GitHub Actions secrets (not committed). From your repo page:
   `Settings → Secrets and variables → Actions`, then add:
   | Secret | Value |
   |--------|-------|
   | `TELEGRAM_BOT_TOKEN` | Your bot token (`123:ABC...`) |
   | `TELEGRAM_CHAT_ID` | The chat/channel ID |
   | `GMGN_API_KEY` | Your GMGN API key |
   | `GMGN_PRIVATE_KEY` | The full PEM private key (BEGIN/END lines included) |

   If you don't have a GMGN API key yet, the CLI generates one:
   `gmgn-cli config` prints a key-link; `gmgn-cli config --apply <KEY>`
   completes it. The private key pairs with that API key.

2. **Push these files to your repo root:**
   - `bot.py`, `config.py`, `target_wallets.txt`, `.env` (optional — env vars
     from secrets override it), `requirements.txt`
   - `.github/workflows/bot.yml` (from this folder's `bot.yml`)

3. **Enable the workflow.** GitHub Actions schedule runs it automatically.
   You can also trigger manually from the repo: `Actions → bot → Run workflow`.

## A note on frequency (the honest trade-off)

The `--once` run does a full scan + any due ROI updates, and GitHub Actions
reports **~1.5 min per run** even when the work is quick (job bootstrap +
runner spin-up dominate). The free private-repo budget is **2000 minutes/month**.

The `bot.yml` default is a cron at **:03 and :33 of every hour** = every 30
minutes, 24/7:

```
48 runs/day × 30 days = 1440 runs × 1.5 min ≈ 2160 min/month
```

That errs slightly over the 2000-min budget on paper, but night-time runs are
faster (~1.0 min, fewer active tokens), which brings most months under. If you
want a guaranteed-safe margin, drop one of the two cron lines (→ ~1080 min/mo)
or move to `"*/45 * * * *"`. The manual `workflow_dispatch` trigger lets you run
it instantly on demand without waiting for the schedule.

## State persistence (critical)

Actions gives you a fresh VM every run, so `bot_state.json` + `alert_track.json`
start empty each time **unless you persist them**. Without persistence:
- dedupe breaks → you re-alert the same tokens;
- ROI tracking restarts → updates compare against a new baseline, not the
  original alert price.

The workflow commits these two files back to a `bot-state` branch after each
run (committed only when they actually change — otherwise you'd generate a
commit every run). Branch tip is fetched before the run (so local state = last
run's state) and pushed after. Race note: `concurrency: bot` ensures only one
run is active at a time, so the branch tip is never clobbered by two runs.

**First run:** the branch doesn't exist yet; `bot.py` treats a missing
`bot_state.json`/`alert_track.json` as "no state" and starts clean — that's
fine.

## Troubleshooting

- **`gmgn-cli: command not found`** → the install step on ubuntu-latest uses
  `npm install -g gmgn-cli`; if Node/npm aren't on PATH the run fails early.
  The workflow pins `setup-node` before it, so this shouldn't happen. If it
  does, check the run log's first few steps.
- **IPv6 401/403** → GitHub's ubuntu runners are IPv4 by default; if you ever
  see IPv6-related auth failures, the CLI can't use IPv6 — this is a runner
  config issue, not your code.
- **Schedules can be delayed** by GitHub queue load (sometimes 15–60 min).
  This is the nature of free CI; the manual `workflow_dispatch` button is your
  on-demand escape hatch.
- **Missing state branch after first run** → the commit step fails gracefully
  if it can't detect a change; state just isn't synced that run. Check the log
  for the "no state change" note.