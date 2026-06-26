"""
Baseball-mentions Kalshi market monitor — persistent loop.

Runs as a systemd service. Checks markets on an adaptive schedule:
  > 3 hours to first pitch : every 30 min
  1–3 hours                : every 10 min
  30–60 min                : every 5 min
  < 30 min (taker window)  : every 1 min
  All games started        : sleep until 9am ET tomorrow

For each national-broadcast game:
  - Places maker orders (1c inside ask) where edge >= MIN_EDGE
  - Cancels and reprices resting orders if market moves > REPRICE_THRESH
  - Switches to taker within TAKER_WINDOW minutes of first pitch
  - Enforces portfolio-relative risk limits on every new bet
"""

import ctypes, gc, json, os, pickle, sys, time, urllib.request, urllib.parse
from datetime import date, datetime, timezone, timedelta

import numpy as np

from kalshi import KalshiClient
from bet_today import (
    fetch_today_games, build_feature_rows, predict_kalshi,
    build_event_ticker, fetch_kalshi_markets_for_game,
    kelly_contracts, _maker_yes_cents, TICKER_SUFFIX_TO_WORD, ALLOWED_WORDS,
    KALSHI_TAKER_FEE,
)

# ── Config ────────────────────────────────────────────────────────────────────

KALSHI_KEY_ID   = os.getenv('KALSHI_KEY_ID',  '')
KALSHI_KEY_PATH = os.getenv('KALSHI_KEY_PATH', '/root/baseball-mentions/kalshi_key.txt')
MODEL_PATH      = 'models/kalshi_models.pkl'
STATE_FILE      = '/root/baseball-mentions/monitor_state.json'

MIN_EDGE       = 0.08   # minimum edge to place a bet
REPRICE_THRESH = 0.03   # reprice resting order if market moved >3c
TAKER_WINDOW   = 25     # minutes before first pitch to switch to taker

# ── Risk limits ───────────────────────────────────────────────────────────────
MIN_BALANCE_PCT     = 0.20  # stop betting if balance < 20% of day-start balance
MAX_EXPOSURE_PCT    = 0.40  # max 40% of current balance in open positions
MAX_POSITIONS       = 20    # max concurrent open positions (count)

ET = timezone(timedelta(hours=-4))  # EDT; update to -5 after Nov DST

TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg_send(text: str):
    """Send a message to the owner's Telegram chat."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        body = json.dumps({'chat_id': TELEGRAM_CHAT_ID, 'text': text,
                           'parse_mode': 'Markdown'}).encode()
        req  = urllib.request.Request(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data=body, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f'  WARN tg_send: {e}')


def tg_get_commands(last_update_id: int) -> tuple[list[str], int]:
    """
    Poll for new Telegram messages. Returns (list of command strings, new last_update_id).
    Only returns messages newer than last_update_id.
    """
    if not TELEGRAM_TOKEN:
        return [], last_update_id
    try:
        params = urllib.parse.urlencode({'offset': last_update_id + 1, 'limit': 10, 'timeout': 0})
        req    = urllib.request.Request(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?{params}')
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        commands = []
        new_id   = last_update_id
        for update in data.get('result', []):
            new_id = max(new_id, update['update_id'])
            text   = update.get('message', {}).get('text', '')
            if text.startswith('/'):
                commands.append(text.split()[0].lower())
        return commands, new_id
    except Exception as e:
        print(f'  WARN tg_get_commands: {e}')
        return [], last_update_id

# ── Adaptive sleep ────────────────────────────────────────────────────────────

def sleep_seconds(min_mins_to_pitch: float) -> int:
    """How long to sleep based on proximity to first pitch."""
    if min_mins_to_pitch > 180:   return 1800   # 30 min
    if min_mins_to_pitch > 60:    return 600    # 10 min
    if min_mins_to_pitch > 30:    return 300    # 5 min
    if min_mins_to_pitch > -30:   return 60     # 1 min (taker window + just after start)
    return secs_until_tomorrow()                # all games started — sleep overnight

def secs_until_tomorrow() -> int:
    """Seconds until 9am ET tomorrow."""
    now   = datetime.now(ET)
    tmrw  = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return max(60, int((tmrw - now).total_seconds()))

# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    today = str(date.today())
    if os.path.exists(STATE_FILE):
        try:
            s = json.loads(open(STATE_FILE).read())
            if s.get('date') == today:
                return s
        except Exception:
            pass
    return {'date': today, 'orders': {}, 'daily_spend': 0.0, 'day_start_balance': None}

def save_state(state: dict):
    open(STATE_FILE, 'w').write(json.dumps(state, indent=2))

def open_exposure(state: dict) -> float:
    return sum(order_cost(rec) for rec in state['orders'].values())

def order_cost(rec: dict) -> float:
    """Dollar cost of a resting order (qty × price per contract)."""
    qty       = rec.get('qty', 1)
    yes_cents = rec.get('yes_cents', 50)
    side      = rec.get('side', 'yes')
    price     = (yes_cents if side == 'yes' else 100 - yes_cents) / 100
    return qty * price


def check_limits(balance: float, state: dict, n_positions: int | None = None) -> tuple[bool, str]:
    day_start = state.get('day_start_balance') or balance
    min_bal   = day_start * MIN_BALANCE_PCT
    max_exp   = balance   * MAX_EXPOSURE_PCT
    spent     = state.get('daily_spend', 0)
    n_pos     = n_positions if n_positions is not None else len(state['orders'])
    if balance <= min_bal:
        return False, f'balance ${balance:.2f} ≤ floor ${min_bal:.2f} ({MIN_BALANCE_PCT:.0%} of ${day_start:.2f})'
    if spent >= max_exp:
        return False, f'exposure ${spent:.2f} ≥ limit ${max_exp:.2f} ({MAX_EXPOSURE_PCT:.0%} of balance)'
    if n_pos >= MAX_POSITIONS:
        return False, f'{n_pos} positions ≥ limit {MAX_POSITIONS}'
    return True, ''

# ── Open orders ───────────────────────────────────────────────────────────────

def get_live_orders(client: KalshiClient) -> dict:
    try:
        resp = client._get('/portfolio/orders', params={'status': 'resting'})
        return {o['order_id']: o for o in resp.get('orders', [])
                if 'KXMLBMENTION' in o.get('ticker', '')}
    except Exception as e:
        print(f'  WARN get_live_orders: {e}')
        return {}

def get_portfolio_positions(client: KalshiClient) -> dict:
    """Returns {ticker: {'exposure': float, 'position': float}} for unsettled KXMLBMENTION positions with exposure > 0."""
    try:
        resp = client._get('/portfolio/positions', params={'settlement_status': 'unsettled'})
        result = {}
        for p in resp.get('market_positions', []):
            ticker = p.get('ticker', '')
            exp    = float(p.get('market_exposure_dollars', 0))
            pos    = float(p.get('position_fp', 0))
            if 'KXMLBMENTION' in ticker and exp > 0:
                result[ticker] = {'exposure': exp, 'position': pos}
        return result
    except Exception as e:
        print(f'  WARN get_portfolio_positions: {e}')
        return {}

def get_settlements_for_tickers(client: KalshiClient, tickers: set) -> dict:
    """Fetch recent settlements; return {ticker: settlement_dict} for the given ticker set."""
    try:
        return {s['ticker']: s for s in client.get_settlements()
                if s.get('ticker') in tickers}
    except Exception as e:
        print(f'  WARN get_settlements_for_tickers: {e}')
        return {}

_MONTH_INV  = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
               'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
_KALSHI_MLB  = {'AZ': 'ARI'}          # Kalshi abbr → MLB abbr (only exceptions)
_2CHAR_TEAMS = {'TB', 'AZ', 'SF', 'SD'}  # Kalshi team codes that are 2 chars

def parse_event_ticker(event_tk: str) -> tuple:
    """Parse 'KXMLBMENTION-26MAY07NYYBOS' → (date, away, home). Returns (None,'?','?') on failure."""
    try:
        code  = event_tk.split('-', 1)[1]          # '26MAY07NYYBOS'
        yy, mon_str, dd = int(code[:2]), code[2:5], int(code[5:7])
        teams = code[7:]
        g_date = date(2000 + yy, _MONTH_INV[mon_str], dd)
        if len(teams) == 4:                        # both 2-char (e.g. TB@SF)
            away, home = teams[:2], teams[2:]
        elif len(teams) == 5:
            if teams[:2] in _2CHAR_TEAMS:          # 2-char away (e.g. TB@BOS)
                away, home = teams[:2], teams[2:]
            else:                                  # 2-char home (e.g. NYY@AZ)
                away, home = teams[:3], teams[3:]
        else:                                      # default 3+3 (e.g. NYY@BOS)
            away, home = teams[:3], teams[3:]
        away = _KALSHI_MLB.get(away, away)
        home = _KALSHI_MLB.get(home, home)
        return g_date, away, home
    except Exception:
        return None, '?', '?'

def seed_state_from_kalshi(state: dict, live_orders: dict):
    """Adopt any live Kalshi orders not already tracked in state."""
    added = 0
    for oid, o in live_orders.items():
        if oid in state['orders']:
            continue
        ticker      = o.get('ticker', '')
        side        = o.get('side', '')
        yes_price_d = o.get('yes_price_dollars')
        qty         = int(float(o.get('remaining_count_fp', 1)))
        if ticker and side and yes_price_d is not None:
            yes_cents = round(float(yes_price_d) * 100)
            cost = qty * (yes_cents if side == 'yes' else 100 - yes_cents) / 100
            state['orders'][oid] = {
                'ticker': ticker, 'side': side, 'qty': qty,
                'yes_cents': yes_cents, 'order_id': oid,
            }
            state['daily_spend'] = state.get('daily_spend', 0.0) + cost
            added += 1
    if added:
        print(f'  Seeded {added} externally-placed order(s) into state')

# ── Price helpers ─────────────────────────────────────────────────────────────

def maker_yes_cents_for_side(mkt: dict, side: str) -> int | None:
    if side == 'yes':
        return _maker_yes_cents(mkt)
    try:
        no_ask_c = round(float(mkt.get('no_ask_dollars', 0)) * 100)
        return min(99, max(1, 100 - (no_ask_c - 1)))
    except (TypeError, ValueError):
        return None

def taker_yes_cents_for_side(mkt: dict, side: str) -> int | None:
    try:
        if side == 'yes':
            return round(float(mkt.get('yes_ask_dollars', 0)) * 100)
        no_ask_c = round(float(mkt.get('no_ask_dollars', 0)) * 100)
        return max(1, 100 - no_ask_c)
    except (TypeError, ValueError):
        return None

def minutes_to_first_pitch(game_time_iso: str) -> float:
    if not game_time_iso:
        return 999.0
    try:
        fp  = datetime.fromisoformat(game_time_iso.replace('Z', '+00:00'))
        return (fp - datetime.now(timezone.utc)).total_seconds() / 60.0
    except Exception:
        return 999.0

# ── Single monitoring pass ────────────────────────────────────────────────────

def run_once(bundle: dict, prev_game_statuses: dict, paused: bool = False) -> tuple[float, str, dict]:
    """
    Run one full monitoring pass. Returns (min_mins_to_pitch, tg_summary, new_game_statuses).
    tg_summary is empty string if nothing notable happened.
    """
    now_et = datetime.now(ET)
    print(f'\n=== {now_et.strftime("%Y-%m-%d %H:%M ET")} ===')



    state = load_state()

    game_date = date.today()
    games     = fetch_today_games(game_date)
    if not games:
        print('  No games today — no Kalshi markets possible, sleeping until tomorrow.')
        return -9999.0, '', prev_game_statuses

    df = build_feature_rows(games, game_date)
    if df.empty:
        print('  No feature rows — sleeping until tomorrow.')
        return -9999.0, '', prev_game_statuses

    df = predict_kalshi(df, bundle)

    client  = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH)
    balance = client.get_balance()

    live_orders         = get_live_orders(client)
    live_order_ids      = set(live_orders.keys())
    portfolio_positions = get_portfolio_positions(client)
    seed_state_from_kalshi(state, live_orders)

    if state.get('day_start_balance') is None:
        state['day_start_balance'] = balance

    # ── Settlement detection ──────────────────────────────────────────────────
    # Snapshot what was unsettled last loop; update to current; diff to find settled.
    # None on first run → skip notification to avoid false positives on startup.
    _prev_unsettled = state.get('prev_unsettled')
    state['prev_unsettled'] = {
        t: {'side': 'yes' if info['position'] > 0 else 'no', 'exposure': info['exposure']}
        for t, info in portfolio_positions.items()
    }
    if _prev_unsettled is not None:
        newly_settled = set(_prev_unsettled.keys()) - set(portfolio_positions.keys())
        if newly_settled:
            print(f'  {len(newly_settled)} position(s) settled since last check: {newly_settled}')
            settle_data = get_settlements_for_tickers(client, newly_settled)
            lines = []
            game_pnl = 0.0
            # Derive game label from any settled ticker (all belong to the same event)
            sample_tk = next(iter(sorted(newly_settled)))
            parts     = sample_tk.split('-')
            event_tk  = parts[0] + '-' + parts[1] if len(parts) >= 2 else sample_tk
            _, away, home = parse_event_ticker(event_tk)
            game_label = f'{away}@{home}' if away != '?' else event_tk
            for ticker in sorted(newly_settled):
                suffix   = ticker.split('-')[-1]
                word     = TICKER_SUFFIX_TO_WORD.get(suffix, suffix.lower())
                side_str = 'YES' if _prev_unsettled[ticker].get('side') == 'yes' else 'NO'
                s        = settle_data.get(ticker)
                if s:
                    yes_qty  = float(s.get('yes_count_fp', 0))
                    no_qty   = float(s.get('no_count_fp', 0))
                    yes_cost = float(s.get('yes_total_cost_dollars', 0))
                    no_cost  = float(s.get('no_total_cost_dollars', 0))
                    fee      = float(s.get('fee_cost', 0))
                    result   = s.get('market_result', '')
                    payout   = yes_qty if result == 'yes' else no_qty
                    cost     = yes_cost if yes_qty > 0 else no_cost
                    profit   = payout - cost - fee
                    game_pnl += profit
                    if profit > 0:
                        lines.append(f'✅ *{word}* {side_str} → WON +${profit:.2f}')
                    elif profit < 0:
                        lines.append(f'❌ *{word}* {side_str} → LOST -${abs(profit):.2f}')
                    else:
                        lines.append(f'↩️ *{word}* {side_str} → VOID/PUSH')
                else:
                    lines.append(f'⏳ *{word}* {side_str} → pending finalization')
            game_pnl_str = f'+${game_pnl:.2f}' if game_pnl >= 0 else f'-${abs(game_pnl):.2f}'
            game_icon    = '📈' if game_pnl >= 0 else '📉'
            tg_send(
                f'🏁 *Markets Settled — {game_label} — {datetime.now(ET).strftime("%H:%M ET")}*\n'
                + '\n'.join(lines)
                + f'\n\n{game_icon} Game P&L: {game_pnl_str}'
            )

    # ── Game-ended detection ──────────────────────────────────────────────────
    # Compare current MLB game statuses to previous loop to find newly-Final games.
    game_status_map = {}
    for g in games:
        pk     = str(g.get('gamePk', ''))
        status = g.get('status', {}).get('abstractGameState', '')
        away   = g.get('teams', {}).get('away', {}).get('team', {}).get('abbreviation', '?')
        home   = g.get('teams', {}).get('home', {}).get('team', {}).get('abbreviation', '?')
        if pk:
            game_status_map[pk] = {'away': away, 'home': home, 'status': status, 'game_date': game_date}

    # Also fetch prior-day games when positions from those dates are still open,
    # so games that ran past midnight are still tracked across the day boundary.
    prior_dates: set = set()
    for ticker in portfolio_positions:
        parts = ticker.split('-')
        if len(parts) >= 2:
            g_date_pos, _, _ = parse_event_ticker(parts[0] + '-' + parts[1])
            if g_date_pos and g_date_pos != game_date:
                prior_dates.add(g_date_pos)
    for prior_date in sorted(prior_dates):
        for g in fetch_today_games(prior_date):
            pk = str(g.get('gamePk', ''))
            if pk and pk not in game_status_map:
                status = g.get('status', {}).get('abstractGameState', '')
                away   = g.get('teams', {}).get('away', {}).get('team', {}).get('abbreviation', '?')
                home   = g.get('teams', {}).get('home', {}).get('team', {}).get('abbreviation', '?')
                game_status_map[pk] = {'away': away, 'home': home, 'status': status, 'game_date': prior_date}


    # Authoritative exposure: filled positions (from Kalshi portfolio) + unfilled resting costs
    filled_exp  = sum(p['exposure'] for p in portfolio_positions.values())
    resting_exp = sum(
        float(o.get('remaining_count_fp', 0)) * (
            float(o.get('yes_price_dollars', 0.5)) if o.get('side') == 'yes'
            else 1.0 - float(o.get('yes_price_dollars', 0.5))
        )
        for o in live_orders.values()
    )
    exposure = filled_exp + resting_exp
    state['daily_spend'] = exposure

    # High water mark: ratchet up to the peak cash balance seen today
    if balance > state['day_start_balance']:
        state['day_start_balance'] = balance

    # Position count: unique tickers with filled exposure + resting-only orders
    filled_tickers  = set(portfolio_positions.keys())
    resting_tickers = {o['ticker'] for o in live_orders.values() if o.get('ticker') not in filled_tickers}
    n_positions     = len(filled_tickers) + len(resting_tickers)

    day_start = state['day_start_balance']
    max_exp   = balance   * MAX_EXPOSURE_PCT
    min_bal   = day_start * MIN_BALANCE_PCT

    print(f'  Balance: ${balance:.2f}  |  Floor: ${min_bal:.2f} ({MIN_BALANCE_PCT:.0%})')
    print(f'  At risk: ${exposure:.2f}  |  exp limit ${max_exp:.2f} ({MAX_EXPOSURE_PCT:.0%})'
          f'  |  Positions: {n_positions}/{MAX_POSITIONS}')
    if portfolio_positions:
        pos_parts = []
        for ticker, info in portfolio_positions.items():
            word_code   = ticker.split('-')[-1]
            side_str    = 'YES' if info['position'] > 0 else 'NO'
            filled_qty  = abs(info['position'])
            rest_qty    = sum(float(o.get('remaining_count_fp', 0)) for o in live_orders.values() if o.get('ticker') == ticker)
            qty_str     = f'{filled_qty:.2f} filled + {rest_qty:.2f} waiting' if rest_qty > 0 else f'{filled_qty:.2f} contracts'
            pos_parts.append(f'{word_code} {side_str} [{qty_str}] ${info["exposure"]:.2f}')
        print(f'  {" | ".join(pos_parts)}')

    can_bet_global, limit_reason = check_limits(balance, state, n_positions)
    if not can_bet_global:
        print(f'  ⛔ Betting paused: {limit_reason}')
    if paused:
        print('  ⏸ Betting manually paused.')

    state_by_ticker: dict[str, dict] = {
        rec['ticker']: rec for rec in state['orders'].values() if 'ticker' in rec
    }

    # ── Game-start position snapshot ──────────────────────────────────────────
    gamestart_notified = set(state.get('gamestart_notified', []))
    for _, game_row in df.iterrows():
        away      = game_row['away_team']
        home      = game_row['home_team']
        game_time = game_row.get('game_time', '')
        mins      = minutes_to_first_pitch(game_time)
        if mins > 0 or mins < -30:
            continue
        event_ticker = build_event_ticker(game_date, away, home)
        if event_ticker in gamestart_notified:
            continue
        gamestart_notified.add(event_ticker)
        game_pos = {t: p for t, p in portfolio_positions.items()
                    if t.startswith(event_ticker + '-')}
        if not game_pos:
            continue
        lines          = []
        total_exposure = 0.0
        for ticker, pos_info in sorted(game_pos.items()):
            suffix   = ticker.split('-')[-1]
            word     = TICKER_SUFFIX_TO_WORD.get(suffix, suffix.lower())
            side_str = 'YES' if pos_info['position'] > 0 else 'NO'
            qty      = abs(pos_info['position'])
            exposure = pos_info['exposure']
            total_exposure += exposure
            e        = state_by_ticker.get(ticker, {}).get('edge')
            edge_str = f' (e {e:.0%})' if e is not None else ''
            lines.append(f'  *{word}* {side_str} {qty:.0f}x ${exposure:.2f}{edge_str}')
        tg_send(
            f'⚾ *{away}@{home} — Game Starting*\n'
            + '\n'.join(lines)
            + f'\n\n${total_exposure:.2f} at risk'
        )
    state['gamestart_notified'] = list(gamestart_notified)

    # ── 3-hour mid-game position update ──────────────────────────────────────
    midgame_notified = set(state.get('midgame_notified', []))
    for _, game_row in df.iterrows():
        away      = game_row['away_team']
        home      = game_row['home_team']
        game_time = game_row.get('game_time', '')
        mins      = minutes_to_first_pitch(game_time)
        if mins > -180:
            continue
        event_ticker = build_event_ticker(game_date, away, home)
        if event_ticker in midgame_notified:
            continue
        game_pos = {t: p for t, p in portfolio_positions.items()
                    if t.startswith(event_ticker + '-')}
        if not game_pos:
            continue
        lines = []
        total_cur_val  = 0.0
        total_exposure = 0.0
        for ticker, pos_info in sorted(game_pos.items()):
            suffix   = ticker.split('-')[-1]
            word     = TICKER_SUFFIX_TO_WORD.get(suffix, suffix.lower())
            side     = 'yes' if pos_info['position'] > 0 else 'no'
            side_str = 'YES' if side == 'yes' else 'NO'
            qty      = abs(pos_info['position'])
            exposure = pos_info['exposure']
            total_exposure += exposure
            try:
                mkt = client.get_market(ticker)
                mid = KalshiClient.implied_prob(mkt)
            except Exception:
                mid = None
            if mid is not None:
                cur_val  = qty * (mid if side == 'yes' else (1.0 - mid))
                total_cur_val += cur_val
                pnl      = cur_val - exposure
                pnl_str  = f'+${pnl:.2f}' if pnl >= 0 else f'-${abs(pnl):.2f}'
                price_str = f'{round(mid * 100)}¢'
                icon     = '📈' if pnl >= 0 else '📉'
                lines.append(f'{icon} *{word}* {side_str} @ {price_str}  ({pnl_str})')
            else:
                lines.append(f'[~] *{word}* {side_str} @ ?')
        if lines:
            total_pnl     = total_cur_val - total_exposure
            total_pnl_str = f'+${total_pnl:.2f}' if total_pnl >= 0 else f'-${abs(total_pnl):.2f}'
            game_icon     = '📈' if total_pnl >= 0 else '📉'
            tg_send(
                f'⏱ *{away}@{home} — 3h Check*\n'
                + '\n'.join(lines)
                + f'\n\n{game_icon} Mark-to-market: {total_pnl_str}'
            )
            midgame_notified.add(event_ticker)
    state['midgame_notified'] = list(midgame_notified)

    min_mins  = float('inf')
    all_game_mins = []
    game_info = []   # (away, home, mins, n_markets) for games with active Kalshi markets

    for _, game_row in df.iterrows():
        away      = game_row['away_team']
        home      = game_row['home_team']
        game_time = game_row.get('game_time', '')

        event_ticker = build_event_ticker(game_date, away, home)
        mins         = minutes_to_first_pitch(game_time)
        use_taker    = mins <= TAKER_WINDOW

        word_markets = fetch_kalshi_markets_for_game(client, event_ticker)
        time.sleep(0.3)
        if not word_markets:
            continue

        min_mins = min(min_mins, mins)
        all_game_mins.append(mins)
        game_info.append((away, home, mins, event_ticker))
        label    = f'{away}@{home}'
        print(f'\n  {label}  ({mins:.0f} min to pitch{"  ⏰ TAKER MODE" if use_taker else ""})')

        for word, mkt in word_markets.items():
            if word not in ALLOWED_WORDS:
                continue
            col     = 'p_' + word.replace(' ', '_')
            model_p = float(game_row.get(col, np.nan))
            if np.isnan(model_p):
                continue

            market_p = KalshiClient.implied_prob(mkt)
            if market_p is None:
                continue

            ticker = mkt.get('ticker', '')
            side   = 'yes' if model_p > market_p else 'no'
            edge   = (model_p - market_p) if side == 'yes' else (1 - model_p) - (1 - market_p)

            # ── No edge: cancel any resting order ────────────────────────────
            if edge < MIN_EDGE:
                if ticker in state_by_ticker:
                    oid = state_by_ticker[ticker].get('order_id')
                    if oid and oid in live_order_ids:
                        print(f'    {word:<14}  edge gone ({edge:.1%}) — cancelling')
                        try:
                            client.cancel_order(oid)
                            live_order_ids.discard(oid)
                        except Exception as e:
                            print(f'      WARN: {e}')
                        cancelled = state['orders'].pop(oid, None)
                        if cancelled:
                            old_cost = order_cost(cancelled)
                            state['daily_spend'] = max(0.0, state.get('daily_spend', 0) - old_cost)
                        state_by_ticker.pop(ticker, None)
                continue

            # ── Kelly sizing ──────────────────────────────────────────────────
            eff_p = model_p if side == 'yes' else 1 - model_p
            try:
                ask_d = float((mkt.get('yes_ask_dollars') if side == 'yes'
                               else mkt.get('no_ask_dollars')) or 0.5)
            except (TypeError, ValueError):
                ask_d = 0.5
            kelly_qty = kelly_contracts(eff_p, ask_d, balance,
                                        fee_rate=KALSHI_TAKER_FEE if use_taker else 0.0)
            qty = max(1, kelly_qty)  # floor at 1 for existing-order management

            # ── Already tracking this ticker ──────────────────────────────────
            if ticker in state_by_ticker:
                existing      = state_by_ticker[ticker]
                oid           = existing.get('order_id')
                old_price     = existing.get('yes_cents', 0)
                still_resting = oid in live_order_ids

                if use_taker and still_resting:
                    print(f'    {word:<14}  switching to taker')
                    try:
                        client.cancel_order(oid)
                        live_order_ids.discard(oid)
                        cancelled = state['orders'].pop(oid, None)
                        if cancelled:
                            old_cost = order_cost(cancelled)
                            state['daily_spend'] = max(0.0, state.get('daily_spend', 0) - old_cost)
                        state_by_ticker.pop(ticker, None)
                    except Exception as e:
                        print(f'      WARN cancel: {e}')
                    can_bet, reason = check_limits(balance, state)
                    if not can_bet:
                        print(f'      SKIP taker ({reason})')
                        continue
                    yc = taker_yes_cents_for_side(mkt, side)
                    if yc:
                        cost   = qty * (yc if side == 'yes' else 100 - yc) / 100
                        result = client.place_order(ticker, side, yc, qty, dry_run=False)
                        oid2   = result.get('order', {}).get('order_id')
                        status = result.get('order', {}).get('status', '')
                        print(f'      taker {side} x{qty} @ {yc}c  cost=${cost:.2f}  status={status}')
                        if oid2:
                            rec = {'ticker': ticker, 'side': side, 'qty': qty,
                                   'yes_cents': yc, 'order_id': oid2, 'edge': edge}
                            state['orders'][oid2]   = rec
                            state_by_ticker[ticker] = rec
                            state['daily_spend']    = state.get('daily_spend', 0) + cost

                elif still_resting:
                    new_yc = maker_yes_cents_for_side(mkt, side)
                    if new_yc and abs(new_yc - old_price) >= int(REPRICE_THRESH * 100):
                        print(f'    {word:<14}  repricing {old_price}c → {new_yc}c')
                        try:
                            client.cancel_order(oid)
                            live_order_ids.discard(oid)
                            cancelled = state['orders'].pop(oid, None)
                            if cancelled:
                                old_cost = order_cost(cancelled)
                                state['daily_spend'] = max(0.0, state.get('daily_spend', 0) - old_cost)
                        except Exception as e:
                            print(f'      WARN cancel: {e}')
                        result = client.place_order(ticker, side, new_yc, qty, dry_run=False)
                        oid2   = result.get('order', {}).get('order_id')
                        status = result.get('order', {}).get('status', '')
                        new_cost = qty * (new_yc if side == 'yes' else 100 - new_yc) / 100
                        print(f'      maker {side} x{qty} @ {new_yc}c  cost=${new_cost:.2f}  status={status}')
                        if oid2:
                            rec = {'ticker': ticker, 'side': side, 'qty': qty,
                                   'yes_cents': new_yc, 'order_id': oid2, 'edge': edge}
                            state['orders'][oid2]   = rec
                            state_by_ticker[ticker] = rec
                            state['daily_spend']    = state.get('daily_spend', 0) + new_cost
                    else:
                        print(f'    {word:<14}  {side} edge={edge:.1%}  [resting @ {old_price}c]')
                else:
                    print(f'    {word:<14}  {side} edge={edge:.1%}  [filled]')
                    if not existing.get('fill_notified'):
                        existing['fill_notified'] = True
                continue

            # ── New bet ───────────────────────────────────────────────────────
            if paused:
                print(f'    {word:<14}  SKIP (manually paused)')
                continue
            if kelly_qty < 1:
                print(f'    {word:<14}  {side} edge={edge:.1%}  [Kelly 0, skip]')
                continue
            can_bet, reason = check_limits(balance, state)
            if not can_bet:
                print(f'    {word:<14}  SKIP ({reason})')
                continue

            yc   = taker_yes_cents_for_side(mkt, side) if use_taker else maker_yes_cents_for_side(mkt, side)
            if not yc:
                continue

            cost   = qty * (yc if side == 'yes' else 100 - yc) / 100
            result = client.place_order(ticker, side, yc, qty, dry_run=False)
            oid2   = result.get('order', {}).get('order_id')
            status = result.get('order', {}).get('status', '')
            mode   = 'taker' if use_taker else 'maker'
            print(f'    {word:<14}  NEW {mode} {side} x{qty} @ {yc}c'
                  f'  edge={edge:.1%}  cost=${cost:.2f}  status={status}')
            if oid2:
                rec = {'ticker': ticker, 'side': side, 'qty': qty,
                       'yes_cents': yc, 'order_id': oid2, 'edge': edge}
                state['orders'][oid2]   = rec
                state_by_ticker[ticker] = rec
                state['daily_spend']    = state.get('daily_spend', 0) + cost

    # Persist game context so /status can group positions by game
    state['game_info'] = [{'away': a, 'home': h, 'event_ticker': et} for a, h, _, et in game_info]

    if min_mins == float('inf'):
        if portfolio_positions:
            # No active markets but still holding open positions — keep polling
            save_state(state)
            return -1.0, '', game_status_map
        print(f'  No Kalshi markets found for any of {len(df)} game(s) today — sleeping until tomorrow.')
        save_state(state)
        return -9999.0, '', game_status_map

    # Recompute sleep signal: min_mins across all games can go very negative once a game
    # starts, incorrectly triggering overnight sleep even when later games are upcoming.
    # Use only games within the actionable window (> -30 min); fall back to short polling
    # if positions are still open, or overnight sleep if truly nothing remains.
    upcoming_or_recent = [m for m in all_game_mins if m > -30]
    if upcoming_or_recent:
        min_mins = min(upcoming_or_recent)
    elif portfolio_positions:
        min_mins = -1   # games underway, positions open — keep checking every minute
    else:
        min_mins = -9999  # all games done, no positions — sleep overnight

    save_state(state)

    # Build Telegram summary — always send when markets are active
    now_str  = datetime.now(ET).strftime("%H:%M") + 'et'
    risk_str = f'${exposure:.2f} risk' if exposure > 0 else 'no pos'

    matched: set[str] = set()

    def _game_positions(event_tk: str) -> list[str]:
        """Return position lines for one game; records matched tickers in `matched`."""
        lines = []
        for pos_tk, info in portfolio_positions.items():
            if not pos_tk.startswith(event_tk + '-'):
                continue
            matched.add(pos_tk)
            word     = TICKER_SUFFIX_TO_WORD.get(pos_tk.split('-')[-1], pos_tk.split('-')[-1].lower())
            sd       = 'Y' if info['position'] > 0 else 'N'
            qty      = abs(info['position'])
            rest_qty = sum(float(o.get('remaining_count_fp', 0))
                           for o in live_orders.values() if o.get('ticker') == pos_tk)
            qty_str  = f'{qty:.0f}x+{rest_qty:.0f}w' if rest_qty > 0 else f'{qty:.0f}x'
            e        = state_by_ticker.get(pos_tk, {}).get('edge')
            edge_str = f' (e {e:.1%})' if e is not None else ''
            lines.append(f'  {word} {sd} {qty_str} ${info["exposure"]:.2f}{edge_str}')
        resting_tks = sorted(
            {o.get('ticker') for o in live_orders.values() if o.get('ticker')}
            - set(portfolio_positions.keys())
        )
        for pos_tk in resting_tks:
            if not pos_tk.startswith(event_tk + '-'):
                continue
            matched.add(pos_tk)
            word          = TICKER_SUFFIX_TO_WORD.get(pos_tk.split('-')[-1], pos_tk.split('-')[-1].lower())
            ticker_orders = [o for o in live_orders.values() if o.get('ticker') == pos_tk]
            sd            = 'Y' if ticker_orders[0].get('side') == 'yes' else 'N'
            rest_qty      = sum(float(o.get('remaining_count_fp', 0)) for o in ticker_orders)
            rest_cost     = sum(
                float(o.get('remaining_count_fp', 0)) * (
                    float(o.get('yes_price_dollars', 0.5)) if o.get('side') == 'yes'
                    else 1.0 - float(o.get('yes_price_dollars', 0.5))
                )
                for o in ticker_orders
            )
            e        = state_by_ticker.get(pos_tk, {}).get('edge')
            edge_str = f' (e {e:.1%})' if e is not None else ''
            lines.append(f'  {word} {sd} {rest_qty:.0f}w ${rest_cost:.2f}{edge_str}')
        return lines

    # Collect all event tickers: today's active games + any game inferred from position tickers
    all_games: dict[str, dict] = {}
    for away, home, mins, event_tk in game_info:
        all_games[event_tk] = {'away': away, 'home': home, 'game_date': game_date, 'mins': mins}
    for pos_tk in (
        set(portfolio_positions.keys()) |
        {o.get('ticker') for o in live_orders.values() if o.get('ticker')}
    ):
        if 'KXMLBMENTION' not in (pos_tk or ''):
            continue
        parts    = pos_tk.split('-')
        event_tk = parts[0] + '-' + parts[1] if len(parts) >= 2 else None
        if event_tk and event_tk not in all_games:
            g_date, away, home = parse_event_ticker(event_tk)
            all_games[event_tk] = {'away': away, 'home': home, 'game_date': g_date, 'mins': None}

    # Order: today's games by mins asc, then other days by date desc
    today_list = sorted(
        [(et, g) for et, g in all_games.items() if g['game_date'] == game_date],
        key=lambda x: x[1].get('mins') or 999,
    )
    other_list = sorted(
        [(et, g) for et, g in all_games.items() if g['game_date'] != game_date],
        key=lambda x: x[1]['game_date'] or date.min, reverse=True,
    )

    blocks = [f'⚾ {now_str}  ${balance:.2f}  {risk_str}']
    for event_tk, g in today_list + other_list:
        away, home, g_date, mins = g['away'], g['home'], g['game_date'], g.get('mins')
        if g_date == game_date and mins is not None:
            if mins > 60:
                t = f'{int(mins // 60)}h{int(mins % 60):02d}m'
            elif mins > 0:
                t = f'{int(mins)}m'
            else:
                t = 'live'
            taker_flag = ' ⏰' if mins <= TAKER_WINDOW else ''
            header = f'*{away}@{home}* {t}{taker_flag}'
        else:
            date_str = g_date.strftime('%-d %b') if g_date else '?'
            header = f'*{away}@{home}* {date_str}'
        game_block = [header]
        game_block.extend(_game_positions(event_tk))
        blocks.append('\n'.join(game_block))

    return min_mins, '\n\n'.join(blocks), game_status_map

# ── Persistent loop ───────────────────────────────────────────────────────────

def main():
    if not KALSHI_KEY_ID:
        print('ERROR: KALSHI_KEY_ID not set'); sys.exit(1)

    print('Baseball monitor starting...')
    tg_send('⚾ Baseball monitor started.')

    with open(MODEL_PATH, 'rb') as fh:
        bundle = pickle.load(fh)
    model_mtime    = os.path.getmtime(MODEL_PATH)
    last_update_id     = 0
    paused             = False
    prev_game_statuses = {}

    while True:
        # ── Reload model if retrain updated it ────────────────────────────────
        mtime = os.path.getmtime(MODEL_PATH)
        if mtime != model_mtime:
            print('  Model file updated — reloading...')
            with open(MODEL_PATH, 'rb') as fh:
                bundle = pickle.load(fh)
            model_mtime = mtime
            tg_send('🔄 Model retrained and reloaded.')

        # ── Run monitoring pass ───────────────────────────────────────────────
        try:
            min_mins, _summary, prev_game_statuses = run_once(bundle, prev_game_statuses, paused=paused)
        except Exception as e:
            msg = f'❌ Monitor error: {e}'
            print(f'  ERROR: {e}')
            tg_send(msg)
            min_mins = 999.0
            prev_game_statuses = {}

        # Return fragmented memory to OS
        gc.collect()
        try:
            ctypes.CDLL('libc.so.6').malloc_trim(0)
        except Exception:
            pass

        secs = sleep_seconds(min_mins)
        wake = datetime.now(ET) + timedelta(seconds=secs)
        print(f'  Sleeping {secs}s (next check ~{wake.strftime("%H:%M ET")})')

        # Sleep in 60s chunks so commands are processed promptly
        while True:
            remaining = (wake - datetime.now(ET)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(60, remaining))

            commands, last_update_id = tg_get_commands(last_update_id)
            for cmd in commands:
                if cmd == '/pause':
                    paused = True
                    tg_send('⏸ Betting paused. Send /resume to restart.')
                    print('  Telegram: paused')
                elif cmd == '/resume':
                    paused = False
                    tg_send('▶️ Betting resumed.')
                    print('  Telegram: resumed')
                elif cmd == '/status':
                    try:
                        _client  = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH)
                        _bal     = _client.get_balance()
                        state    = load_state()
                        ppos     = get_portfolio_positions(_client)
                        lorders  = get_live_orders(_client)
                        real_exp = sum(p['exposure'] for p in ppos.values()) + sum(
                            float(o.get('remaining_count_fp', 0)) * (
                                float(o.get('yes_price_dollars', 0.5)) if o.get('side') == 'yes'
                                else 1.0 - float(o.get('yes_price_dollars', 0.5))
                            )
                            for o in lorders.values()
                        )

                        smatched: set[str] = set()
                        s_edge_by_ticker = {
                            rec['ticker']: rec.get('edge')
                            for rec in state.get('orders', {}).values() if 'ticker' in rec
                        }

                        def _spos(event_tk):
                            lines = []
                            for pos_tk, info in ppos.items():
                                if not pos_tk.startswith(event_tk + '-'):
                                    continue
                                smatched.add(pos_tk)
                                word     = TICKER_SUFFIX_TO_WORD.get(pos_tk.split('-')[-1], pos_tk.split('-')[-1].lower())
                                sd       = 'Y' if info['position'] > 0 else 'N'
                                qty      = abs(info['position'])
                                rest_qty = sum(float(o.get('remaining_count_fp', 0))
                                               for o in lorders.values() if o.get('ticker') == pos_tk)
                                qty_str  = f'{qty:.0f}x+{rest_qty:.0f}w' if rest_qty > 0 else f'{qty:.0f}x'
                                e        = s_edge_by_ticker.get(pos_tk)
                                edge_str = f' (e {e:.1%})' if e is not None else ''
                                lines.append(f'  {word} {sd} {qty_str} ${info["exposure"]:.2f}{edge_str}')
                            for pos_tk in sorted(
                                {o.get('ticker') for o in lorders.values() if o.get('ticker')} - set(ppos.keys())
                            ):
                                if not pos_tk.startswith(event_tk + '-'):
                                    continue
                                smatched.add(pos_tk)
                                word          = TICKER_SUFFIX_TO_WORD.get(pos_tk.split('-')[-1], pos_tk.split('-')[-1].lower())
                                ticker_orders = [o for o in lorders.values() if o.get('ticker') == pos_tk]
                                sd            = 'Y' if ticker_orders[0].get('side') == 'yes' else 'N'
                                rest_qty      = sum(float(o.get('remaining_count_fp', 0)) for o in ticker_orders)
                                rest_cost     = sum(
                                    float(o.get('remaining_count_fp', 0)) * (
                                        float(o.get('yes_price_dollars', 0.5)) if o.get('side') == 'yes'
                                        else 1.0 - float(o.get('yes_price_dollars', 0.5))
                                    )
                                    for o in ticker_orders
                                )
                                e        = s_edge_by_ticker.get(pos_tk)
                                edge_str = f' (e {e:.1%})' if e is not None else ''
                                lines.append(f'  {word} {sd} {rest_qty:.0f}w ${rest_cost:.2f}{edge_str}')
                            return lines

                        now_str  = datetime.now(ET).strftime("%H:%M") + 'et'
                        risk_str = f'${real_exp:.2f} risk' if real_exp > 0 else 'no pos'
                        # Collect all games: stored game_info + inferred from position tickers
                        s_all_games: dict[str, dict] = {}
                        today = date.today()
                        for g in state.get('game_info', []):
                            s_all_games[g['event_ticker']] = {
                                'away': g['away'], 'home': g['home'],
                                'game_date': today, 'is_today': True,
                            }
                        for pos_tk in (
                            set(ppos.keys()) |
                            {o.get('ticker') for o in lorders.values() if o.get('ticker')}
                        ):
                            if 'KXMLBMENTION' not in (pos_tk or ''):
                                continue
                            parts    = pos_tk.split('-')
                            event_tk = parts[0] + '-' + parts[1] if len(parts) >= 2 else None
                            if event_tk and event_tk not in s_all_games:
                                g_date, away, home = parse_event_ticker(event_tk)
                                s_all_games[event_tk] = {
                                    'away': away, 'home': home,
                                    'game_date': g_date, 'is_today': g_date == today,
                                }

                        today_list = sorted(
                            [(et, g) for et, g in s_all_games.items() if g['is_today']],
                            key=lambda x: x[0],
                        )
                        other_list = sorted(
                            [(et, g) for et, g in s_all_games.items() if not g['is_today']],
                            key=lambda x: x[1]['game_date'] or date.min, reverse=True,
                        )

                        spent      = state.get('daily_spend', 0.0)
                        pause_flag = '  ⏸' if paused else ''
                        spend_line = f'risk ${spent:.2f} / ${_bal * MAX_EXPOSURE_PCT:.2f} limit'
                        blocks = [f'⚾ {now_str}  ${_bal:.2f}  {risk_str}{pause_flag}\n{spend_line}']
                        for event_tk, g in today_list + other_list:
                            away, home, g_date = g['away'], g['home'], g['game_date']
                            if g['is_today']:
                                header = f'*{away}@{home}* today'
                            else:
                                date_str = g_date.strftime('%-d %b') if g_date else '?'
                                header = f'*{away}@{home}* {date_str}'
                            game_block = [header]
                            game_block.extend(_spos(event_tk))
                            blocks.append('\n'.join(game_block))

                        tg_send('\n\n'.join(blocks))
                    except Exception as e:
                        tg_send(f'❌ Status error: {e}')
                elif cmd == '/logs':
                    try:
                        lines = open('/var/log/baseball-monitor.log').readlines()
                        tail  = ''.join(lines[-20:])
                        tg_send(f'```\n{tail[-3000:]}\n```')
                    except Exception as e:
                        tg_send(f'Could not read logs: {e}')
                else:
                    tg_send(f'Unknown command: {cmd}\nAvailable: /status /pause /resume /logs')

if __name__ == '__main__':
    main()
