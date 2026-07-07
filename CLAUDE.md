# Baseball Mentions — Kalshi Betting Model

## Maintaining this file

If you discover that anything in this file is wrong or out of date, correct it
immediately — don't just note it in the conversation. If you learn something new
about the codebase, API behaviour, or workflow that isn't captured here, add it.
This file is the primary context for future sessions; keep it accurate.

Predicts whether specific words will be spoken during MLB game broadcasts,
then bets on Kalshi prediction markets when model probability diverges from
market price.

**Kalshi series:** `KXMLBMENTION` — "What will the announcers say during [game]?"
Only nationally televised games get these markets (1–3 per day).

---

## Python version — critical

**Locally:** use `python` (Anaconda 3.9.13 at `/opt/anaconda3/bin/python`).
Never use `python3` — that is the system Python 3.14, which lacks
`cryptography`, `sklearn`, `xgboost`, and breaks on `X | Y` union type hints.
**On the server:** use `/root/baseball-env/bin/python` (a dedicated virtualenv).
This is what cron, systemd, and `retrain.py` all use. Don't use bare `python`
or `python3` in server commands.

When writing new code: use `Optional[X]`, `List`, `Dict` instead of
`list[str]`, `dict[str, X]`, or `X | Y` — keeps it compatible with Python 3.9
on both environments.

---

## Paths

**Local repo:** `/Users/Ari/repos/personal/money/baseball-mentions/`
**Server path:** `/root/baseball-mentions/`
**GitHub repo:** `https://github.com/ateich7/baseball-mentions` (private)

## Version control & deploy workflow

The repo is on GitHub. **Claude handles all commits and deploys** — the user
never needs to run git commands manually.

**Standard deploy (after any code change):**
```bash
# Claude does this automatically:
git add <changed files>
git commit -m "..."
git push
ssh trading-server "cd /root/baseball-mentions && git pull"
```

After a git pull, restart `latency-collect.service` to pick up code changes:
```bash
ssh trading-server "systemctl restart latency-collect"
```
If the prediction monitor is ever reactivated: `systemctl restart baseball-monitor`.

**What is and isn't in git:**

| In git | Not in git (server-only or generated) |
|--------|---------------------------------------|
| All `.py` files | `kalshi_key.txt` (RSA private key) |
| `CLAUDE.md`, `.gitignore` | `retrain.py` (server-only script) |
| `kalshi_outcomes.csv` | `models/*.pkl` (retrained on server) |
| `archive/scripts/`, `archive/data/` | `monitor_state.json` (runtime state) |
| | `data/latency_*.jsonl/.csv` |
| | `archive/transcripts/`, `archive/logs/` |
| | `.claude/settings.local.json` |

**Server-only files** (`kalshi_key.txt`, `retrain.py`, model `.pkl`) live only on
the server and are never committed. To edit `retrain.py`: read it from the server
first, edit locally, then `scp` it over (it's not in git).

**Server git setup:** Uses a deploy key at `~/.ssh/github_deploy` (read-only,
scoped to this repo). SSH config at `~/.ssh/config` routes `github.com` to it.

## All scripts run on the remote server

**Never run `monitor.py` locally.** Kalshi credentials exist only on the server.

SSH — use the config alias (no flags needed):
```bash
ssh trading-server
```

This uses `~/.ssh/claude-server-key` via `~/.ssh/config`. The old key
`weather-trader-server-key` requires a passphrase and cannot be used in
non-interactive sessions. Always use `trading-server`.

Kalshi key file on server: `/root/baseball-mentions/kalshi_key.txt`

---

## Required env vars (every Kalshi script)

```
KALSHI_KEY_ID=5da099c4-b40e-4ce0-a9a0-9f22fad881e5
KALSHI_KEY_PATH=/Users/Ari/repos/personal/money/weather-trader/Ari's Key.txt
# On server:
KALSHI_KEY_PATH=/root/baseball-mentions/kalshi_key.txt
```

---

## Daily workflow (current — latency arbitrage phase)

**`collect_latency_data.py` runs as a systemd service (`latency-collect.service`).**
It starts automatically on boot, restarts on crash, and runs continuously.
Do NOT run it manually — the service manages it.

```bash
# Check status / logs
ssh trading-server "systemctl status latency-collect"
ssh trading-server "tail -50 /var/log/latency-collect.log"

# Restart after a code deploy
ssh trading-server "systemctl restart latency-collect"

# Stop / start manually
ssh trading-server "systemctl stop latency-collect"
ssh trading-server "systemctl start latency-collect"
```

The script auto-discovers national games from Kalshi open events, polls
MLB live feed every 10s, writes events + prices to `data/`. Runs all day,
sleeps overnight, resumes next day.

**After a collection session** (next session, not necessarily same day):
```bash
ssh trading-server
cd /root/baseball-mentions
/root/baseball-env/bin/python analyze_latency.py
```
Prints per-event price movement at T+0/10/30/60/120/300s, cross-market
snapshots, and a summary table of average lag per word.

**Prediction pipeline (inactive — kept for reference):**
```bash
# These all live in prediction/ on both local and server
cd /root/baseball-mentions/prediction
python check_results.py   # open positions + settled P&L
```

**Model retraining (cron PAUSED — manual only if ever needed):**
```bash
ssh trading-server
cd /root/baseball-mentions
KALSHI_KEY_ID=5da099c4-b40e-4ce0-a9a0-9f22fad881e5 \
KALSHI_KEY_PATH=/root/baseball-mentions/kalshi_key.txt \
/root/baseball-env/bin/python retrain.py
```

**Note:** `retrain.py` exists on the server but is NOT in the local repo.
To edit: read from server, edit locally, scp back. Uses `/root/baseball-env/bin/python`.

---

## Pipeline overview

### Active (latency arbitrage)

| File | Where | Role |
|------|-------|------|
| `collect_latency_data.py` | local + server root | Main loop: polls MLB live feed + Kalshi prices every 10s; writes to `data/` |
| `analyze_latency.py` | local + server root | Cross-tabs every logged event type against every market (not just each event's own pre-mapped word), scores each pair against that market's ambient noise floor, ranks known + hidden pairs, breaks down by inning. Run with no args for the default report; `--verbose` for the old per-event dump |
| `fetch_latency_outcomes.py` | local + server root | Fetches real Kalshi settlement results (yes/no) for the specific games in `latency_events.jsonl`, by event ticker → `data/latency_outcomes.csv`. Needs Kalshi credentials — server only. Re-run periodically to pick up newly-settled games; not automatic |
| `analyze_jumps.py` | local + server root | Backtests "opt out once price is already extreme (≥0.90/≤0.10)": accuracy vs. real settlement, whether it reverses, gap/P&L still capturable right after crossing (taker vs. maker), and a threshold sensitivity sweep. Needs `latency_outcomes.csv`. Appends one row per market per run to `data/opt_out_history.csv` (`--no-log` to skip) so results are comparable over time as more games are collected |
| `kalshi.py` | local + server root | `KalshiClient` — RSA-PSS auth, all Kalshi HTTP calls |
| `utils.py` | local + server root | Shared helpers: `get_json`, `ip_to_float`, `fetch_team_stats`, `TICKER_SUFFIX_TO_WORD` |
| `test_pipeline.py` | local + server root | Smoke-tests the data pipeline with synthetic test data |

### Inactive — prediction pipeline (in `prediction/` subdir)

| File | Where | Role |
|------|-------|------|
| `prediction/kalshi_outcomes.py` | local + server | Downloads settled KXMLBMENTION events + MLB features → `data/kalshi_outcomes.csv` |
| `prediction/train_kalshi_model.py` | local + server | Trains logistic regression models → `models/kalshi_models.pkl` |
| `prediction/bet_today.py` | local + server | Fetches today's schedule, builds features, queries Kalshi prices, recommends/places bets |
| `prediction/monitor.py` | local + server | Persistent loop; adaptive polling, maker→taker switch, repricing, Telegram alerts |
| `prediction/check_results.py` | local + server | Shows open positions and settled P&L |
| `prediction/settled_review.py` | local + server | Reviews settled markets |
| `prediction/broadcast_crews.py` | local + server | Static `CREW` dict mapping `(network, year)` → `(announcer_1, announcer_2)` |
| `retrain.py` | **server only** | Cron wrapper: runs outcomes → skips or retrains → Telegram summary |

**Server-only files not in local repo:** `retrain.py`. If editing it, read from
server first (`ssh trading-server cat /root/baseball-mentions/retrain.py`), edit
locally, then scp back.

**Server file structure:** Latency scripts (`collect_latency_data.py`, `analyze_latency.py`,
`fetch_latency_outcomes.py`, `analyze_jumps.py`, `kalshi.py`, `utils.py`) live at
`/root/baseball-mentions/` root. The old prediction pipeline is in
`/root/baseball-mentions/prediction/`.

**`data/latency_outcomes.csv`** and **`data/opt_out_history.csv`** are server-only,
not git-tracked (same treatment as `latency_events.jsonl`/`latency_prices.csv`).
The former is a point-in-time snapshot from the last `fetch_latency_outcomes.py`
run; the latter is append-only across every `analyze_jumps.py` run, so it's the
place to look for how backtest results have moved as more games got collected.

**`.bak` files on server** (`bet_today.py.bak`, `monitor.py.bak`, etc.) are
the previous versions before the last deploy. Safe to ignore; don't sync them locally.

**`archive/` directory** — legacy XGBoost transcript pipeline (no longer used):
- `archive/scripts/` — `train_models.py`, `pipeline.py`, `enrich_data.py`, `add_team_stats.py`, `add_broadcasters.py`, `clean_data.py`, `make_model_features.py`
- `archive/data/` — `game_labels*.csv`, `model_features.csv` (old transcript model datasets)
- `archive/models/final_models.pkl` — old XGBoost model
- `archive/transcripts/` — YouTube game transcripts (~600 games)
- `archive/logs/` — stale `output*.log` from old manual runs
- `archive/server`, `archive/server.key` — old server connection file and SSH key (superseded by `~/.ssh/claude-server-key`)

---

## Model architecture

**Why not XGBoost:** small training set overfits badly; strong L2 logistic
regression with domain-selected features is more robust.

**Current model:** Logistic regression `C=0.05`, trained on Kalshi-settled
outcomes as ground truth. Only words with LOO AUC > 0.58 get an LR model;
all others use the historical base rate (flat constant, same every game).

**Feature set (9 features):**
`series_game_num`, `days_to_trade_deadline`, `temp_f`, `home_wins`, `away_wins`,
`home_sp_era`, `away_sp_era`, `home_ops`, `away_ops`

`is_national` and `day_night_bin` removed from training and inference —
prior analysis showed they were skewing predictions. Removal was applied to
both `train_kalshi_model.py` and `build_kalshi_feature_vector` in `bet_today.py`.

**Current model state (76 training games, retrained 2026-05-22 without is_national/day_night_bin):**

| Word | Type | Base rate | LOO AUC |
|------|------|-----------|---------|
| double play | **lr** | 90.8% | 0.665 |
| grand slam | base_rate | 55.3% | 0.573 |
| bunt | base_rate | 75.0% | 0.542 |
| walk off | base_rate | 51.3% | 0.517 |
| wild pitch | base_rate | 30.3% | 0.517 |
| error | base_rate | 64.5% | 0.506 |
| pitch clock | base_rate | 23.7% | 0.504 |
| mvp | base_rate | 69.7% | 0.562 |
| triple | base_rate | 90.8% | 0.437 |
| bases loaded | base_rate | 61.8% | 0.423 |
| pinch hit | base_rate | 70.5% | 0.424 (44 games) |
| extra inning | base_rate | 35.5% | 0.405 |
| perfect game | base_rate | 18.2% | 0.403 (44 games) |
| trade | base_rate | 89.5% | 0.324 |
| what a catch | base_rate | 6.6% | 0.304 |
| bullpen | base_rate | 95.0% | n/a (44 games) |

Note: removing `is_national`/`day_night_bin` dropped `mvp` from LR (0.628) to
base_rate (0.562). Only `double_play` retains an LR model — but it is NOT in
`ALLOWED_WORDS` (it was never profitable). All 4 `ALLOWED_WORDS` are now
base_rate models — edge comes solely from market mispricing vs historical rate.

`bullpen`, `pinch_hit`, `perfect_game` have only 44 training samples (missing
from early 2026 events).

`models/kalshi_models.pkl` bundle structure:
```python
{'models': {word_spaced: {'type': 'lr'|'base_rate', 'model': lr|None,
                           'scaler': scaler, 'base_rate': float,
                           'loo_auc': float, 'n': int}},
 'num_features': [...], 'scaler': scaler}
```

`archive/models/final_models.pkl` — old XGBoost transcript model, not used.

---

## Kalshi API quirks

- **Prices:** Always use `yes_ask_dollars` / `yes_bid_dollars` (dollar strings).
  The integer `yes_ask` / `yes_bid` cent fields are often null or zero.
- **`previous_yes_ask_dollars` is biased:** This is the last pre-settlement price,
  not the opening price. For YES-resolving markets it drifts toward $1.00 during
  the game. Never use it as a pre-game price proxy.
- **`sub_title` is null.** Word identity comes from `yes_sub_title` or the ticker
  suffix — the suffix is more reliable.
- **`get_series('sports')` returns null** — don't filter by category. Use
  `get_events('KXMLBMENTION', status='settled')` directly.
- **Auth signing:** message = `timestamp_ms + METHOD.upper() + '/trade-api/v2' + path`.
  The `/trade-api/v2` prefix is required — signing just the path causes 401.
- **Maker orders are fee-free.** Taker fee is 7% of profit (not cost).

---

## Ticker format

**Event ticker:** `KXMLBMENTION-{YYMONDD}{AWAY}{HOME}`
Example: `KXMLBMENTION-26MAY07NYYBOS`

**Market ticker:** event ticker + `-` + 4-char word suffix
Example: `KXMLBMENTION-26MAY07NYYBOS-WALK`

**Team abbreviation exception:** Kalshi uses `AZ` for Diamondbacks; MLB uses `ARI`.
Map defined in `MLB_TO_KALSHI` (bet_today.py) and `KALSHI_TO_MLB` (kalshi_outcomes.py).

**Ticker suffix → word** (defined in `utils.TICKER_SUFFIX_TO_WORD`):

| Suffix | Word | Suffix | Word |
|--------|------|--------|------|
| BULL | bullpen | PINC | pinch hit |
| MVP | mvp | PERF | perfect game |
| PITC | pitch clock | GRAN | grand slam |
| EXTR | extra inning | TRAD | trade |
| BASE | bases loaded | TRIP | triple |
| WHAT | what a catch | WALK | walk off |
| BUNT | bunt | ERRO | error |
| WILD | wild pitch | DOUB | double play |
| NQE | *(skip — event doesn't qualify)* | | |

**Active market set has changed over time:**
- Through June 6 2026: all 16 words offered per game
- June 7 2026 onward: only **12 words** — Kalshi dropped `bullpen`, `perfect_game`,
  `triple`, `double_play` permanently. Current active set: `pinch hit`, `mvp`,
  `pitch clock`, `grand slam`, `extra inning`, `trade`, `bases loaded`,
  `what a catch`, `walk off`, `bunt`, `error`, `wild pitch`.
  Code handles this gracefully (missing tickers → silently skipped).

---

## Ticker parsing (2-char team codes)

Kalshi uses 2-char codes for: `KC`, `TB`, `SF`, `AZ`, `SD`.
A greedy parse of e.g. `KCSEA` fails (`KCS` + `EA`). The fix is to try all
split points (2+2, 2+3, 3+2, 3+3) and validate both halves against `KALSHI_TO_MLB`.
This is implemented correctly in `kalshi_outcomes.parse_ticker()` and
`monitor.parse_event_ticker()`.

---

## Betting logic

**Bet sizing:** Quarter-Kelly with hard cap at 5% of balance per word.

**Maker vs taker:**
- Default: post 1¢ inside the ask (maker, fee-free).
- Within 25 minutes of first pitch: switch to taker (lifts ask immediately, 7% fee on profit).

**Monitor risk limits:**
- Stop betting if balance < 20% of day-start balance
- Max 40% of current balance in total open exposure
- Max 20 concurrent positions

**Bet sizing:** Quarter-Kelly × live account balance. `MAX_KELLY_FRAC = 0.50`
caps any single bet at 50% of balance. With ~$50 balance, typical bet: $1–$4.

**Monitor Telegram commands:** `/pause`, `/resume`, `/status`, `/logs`

---

## latency-collect.service lifecycle

**`latency-collect.service` is ACTIVE and ENABLED** (runs `collect_latency_data.py` continuously).

Unit file: `/etc/systemd/system/latency-collect.service`
Log file: `/var/log/latency-collect.log`
Key settings: `Restart=always`, `RestartSec=30`, `MemoryMax=200M`

**Env vars** set in the unit file:
`KALSHI_KEY_ID`, `KALSHI_KEY_PATH=/root/baseball-mentions/kalshi_key.txt`,
`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `PYTHONUNBUFFERED=1`

---

## monitor.py lifecycle

**`baseball-monitor.service` is currently INACTIVE and DISABLED** (stopped June 8, 2026).
The project has pivoted to latency arbitrage; `monitor.py` is not being used.

If ever reactivated:
```bash
ssh trading-server
systemctl start baseball-monitor       # start
systemctl status baseball-monitor      # check
journalctl -u baseball-monitor -f      # live logs
systemctl stop baseball-monitor        # stop
```

Unit file: `/etc/systemd/system/baseball-monitor.service`
Key settings: `Restart=always`, `RestartSec=30`, `RuntimeMaxSec=86400`,
`MemoryMax=200M`, `WorkingDirectory=/root/baseball-mentions`

**Env vars** set in the unit file:
`KALSHI_KEY_ID`, `KALSHI_KEY_PATH=/root/baseball-mentions/kalshi_key.txt`,
`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `PYTHONUNBUFFERED=1`

Log file (if active): `/var/log/baseball-monitor.log`
State file: `/root/baseball-mentions/monitor_state.json`

---

## Telegram alerting

`TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` are env vars set in `latency-collect.service`'s
unit file — no manual setup needed when using the service. If ever running the script
directly outside the service, set them in the shell.

`tg_send()` silently swallows errors — if messages aren't arriving, check for
`WARN tg_send:` in the script's stdout.

---

## Account context

The account peaked at ~$317 (2026-05-16) before a 94% drawdown to $18.11
(2026-05-22). Net P&L on 129 settled bets: -$222.08 on $756.08 wagered (-29% ROI).
Primary causes: May 15 TORDET single-game loss ~$90, persistent bunt NO losses,
MVP volatility at scale. No new bets placed since mid-May; account is dormant
while the latency arbitrage strategy is developed.

Daily cron retrain is paused. No active positions.

Check the current balance on the server:
```bash
ssh trading-server
cd /root/baseball-mentions
KALSHI_KEY_ID=5da099c4-b40e-4ce0-a9a0-9f22fad881e5 \
KALSHI_KEY_PATH=/root/baseball-mentions/kalshi_key.txt \
/root/baseball-env/bin/python -c "
from kalshi import KalshiClient; import os
k = KalshiClient(os.environ['KALSHI_KEY_ID'], os.environ['KALSHI_KEY_PATH'])
print(f'Balance: \${k.get_balance():.2f}')
"
```

---

## Ground truth & training data

Kalshi's `result: yes/no` on each settled market IS the ground truth.
`kalshi_outcomes.py` re-fetches ALL settled events from scratch each run —
new games are included automatically with no manual labeling.

**Known gap:** Early 2026 events (before ~mid-April) only had 13 words —
`bullpen`, `pinch_hit`, `perfect_game` are missing from those rows, giving
them only 44 training samples vs 76 for everything else.

**Sample size:** 76 games as of 2026-05-22. Base rate confidence intervals
are still wide (~±10–12%). The Kalshi market is generally well-calibrated —
edge is found only in a handful of words.

---

## MLB Stats API

Base URL: `https://statsapi.mlb.com/api/v1/`

Key endpoints used:
- `/schedule?sportId=1&date=...&hydrate=team,venue,probablePitcher,broadcasts,linescore,weather,game(seriesStatus)` — today's games
- `/people/{id}/stats?stats=season&season=...&group=pitching&gameType=R` — pitcher stats
- `/teams/{id}/stats?stats=byDateRange&...` — team season stats (hitting + pitching)
- `/game/{gamePk}/boxscore` — starting pitcher IDs, attendance fallback
- **`/api/v1.1/game/{gamePk}/feed/live`** — live play-by-play feed (latency strategy).
  Note: uses `v1.1`, NOT `v1` — the v1 path returns 404. Only available for in-progress
  games; finished games return 404.

Rate limit: sleep 0.15s between calls. `get_json()` retries 3× with exponential backoff.

**Innings pitched notation:** `'7.2'` means 7 innings + 2 outs = 7.667 IP (NOT 7.2).
Use `utils.ip_to_float()` for all IP conversion.

---

## Current status & open priorities

*Update this section at the start of each session to reflect where things stand.*

**As of 2026-06-26:**

**Active strategy: latency arbitrage data collection**
- `collect_latency_data.py` runs continuously via `latency-collect.service` (ACTIVE,
  ENABLED). The service started on June 26 and auto-restarts on crash. Code changes
  require `systemctl restart latency-collect` to take effect.
- All betting monitors are DEAD and DISABLED:
  - `baseball-monitor.service` — stopped June 8, disabled
  - `nba-monitor.service` — stopped June 8, disabled
  - `mlb-monitor.service` — stopped June 14, disabled
- No relevant cron jobs running (daily retrain cron is paused; mlb-outcomes daily
  report cron appears gone too — verify before assuming it still runs).

**Data collected so far:**
- `data/latency_events.jsonl` — 178 events from 5 games (June 14, 20, 21)
- `data/latency_prices.csv` — ~291k price rows from 8+ game matchups, continuously appended

**Active market set (since June 7 2026):** 12 words — Kalshi dropped `bullpen`,
`perfect_game`, `triple`, `double_play` permanently. Current set:
`pinch hit`, `mvp`, `pitch clock`, `grand slam`, `extra inning`, `trade`,
`bases loaded`, `what a catch`, `walk off`, `bunt`, `error`, `wild pitch`.

**MLB hydrate=broadcasts blocked:** The MLB Stats API now returns 406 for
`hydrate=broadcasts`. Game discovery now uses `find_games_from_kalshi()` —
queries Kalshi open KXMLBMENTION events directly, then looks up game_pk from
MLB `/schedule?hydrate=team` (no broadcasts needed). `find_national_games()` is
kept in `collect_latency_data.py` for reference but not called.

**Sibling projects (also inactive):**
- NBA: `/Users/Ari/repos/personal/money/basketball-mentions/` — `nba-monitor.service` stopped
- MLB Outcomes: `/root/mlb-outcomes/` — `mlb-monitor.service` stopped June 14

**Next priorities (verify with user):**
- Run `analyze_latency.py` to measure actual lag on collected data
- Determine if lag is consistently > ~15s (tradeable) or if market reprices too fast
- If tradeable: build execution logic into `collect_latency_data.py` or a new script

---

## MLB Outcomes sibling project

**Local path:** `/Users/Ari/repos/personal/money/mlb-outcomes/`
**Server path:** `/root/mlb-outcomes/`

Separate prototype targeting KXMLBGAME (home wins), KXMLBEXTRAS (extra innings),
KXMLBRFI (first inning run) markets. Covers ALL MLB games, not just national TV.

**Key insight:** Game outcomes have feature causality (win%, ERA, OPS → who wins)
unlike broadcast word mentions where causality is weak.

**Pipeline:**
| File | Role |
|------|------|
| `kalshi_mlb_outcomes.py` | Data collection → `mlb_outcomes.csv` (726 settled games) |
| `train_mlb_model.py` | Train LR models → `models/mlb_models.pkl` |
| `bet_mlb.py` | One-shot daily recommender (dry run or --execute; $5 cap, 1 contract) |
| `monitor_mlb.py` | Persistent monitor (maker→reprice→taker, $5/day cap, 1 contract) |
| `daily_report.py` | Noon ET P&L Telegram report via cron |
| `kalshi.py` | Copied from baseball-mentions (shared Kalshi client) |

**Model state (726 games, as of 2026-05-22):**

| Target | Type | Base rate | LOO AUC |
|--------|------|-----------|---------|
| home_won | **lr** | 51.2% | **0.788** |
| first_inning_run | **lr** | 49.4% | 0.583 |
| went_extras | base_rate | 13.0% | 0.336 (weak) |

**Feature set (13 features):**
Raw: `home_sp_era`, `away_sp_era`, `home_ops`, `away_ops`, `home_r_g`, `away_r_g`,
`temp_f`, `series_game_num`, `is_national`
Derived: `win_pct_diff` (primary signal), `sp_era_diff`, `ops_diff`, `r_g_diff`

**Dry run output (2026-05-22):** 12 bets recommended, edges 5–38%, ~40% exposure cap hit.
Largest edges are NO on home_won for heavily mismatched games (Kalshi appears to price
home advantage without adjusting for team quality differential).

**`split_team_codes` fix:** Validates both halves against `KNOWN_TEAMS` set and
tries 3-char splits before 2-char to avoid garbled codes (e.g., MIN+KC not MI+NKC).

**Systemd service:** `mlb-monitor.service` — **INACTIVE, disabled** (stopped June 14, 2026)

```bash
systemctl status mlb-monitor
journalctl -u mlb-monitor -f
systemctl restart mlb-monitor   # if ever reactivated
```

Log: `/var/log/mlb-monitor.log`
State: `/root/mlb-outcomes/monitor_state.json`
Kalshi key: `/root/mlb-outcomes/kalshi_key.txt`
Telegram bot: @MLB_Outcomes_Bot (`TELEGRAM_CHAT_ID=7181028824`)
Cron: `0 16 * * *` → `daily_report.py` (noon ET P&L summary) — verify still active before relying on it

**Telegram commands:** `/status` `/pause` `/resume` `/logs`

**Monitor strategy:** maker 1¢ inside ask → reprice if drift >3¢ → taker within 25 min of pitch.
$5/day hard cap, 1 contract per game (testing period).

**Usage:**
```bash
# Dry run (no orders placed)
cd /root/mlb-outcomes
KALSHI_KEY_ID=5da099c4-b40e-4ce0-a9a0-9f22fad881e5 \
KALSHI_KEY_PATH=/root/mlb-outcomes/kalshi_key.txt \
/root/baseball-env/bin/python bet_mlb.py --min-edge 0.06

# Refresh data + retrain
KALSHI_KEY_ID=5da099c4-b40e-4ce0-a9a0-9f22fad881e5 \
KALSHI_KEY_PATH=/root/mlb-outcomes/kalshi_key.txt \
/root/baseball-env/bin/python kalshi_mlb_outcomes.py
/root/baseball-env/bin/python train_mlb_model.py
```
