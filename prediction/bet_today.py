"""
bet_today.py — daily pre-game bet recommender.

Loads models/kalshi_models.pkl, builds feature rows for today's MLB games,
queries Kalshi for live prices, sizes bets via quarter-Kelly, and optionally
places maker orders (1¢ inside the ask, no fees) or taker orders (lift ask).

Usage:
    python bet_today.py [--date YYYY-MM-DD] [--min-edge 0.08] [--execute]
    python bet_today.py --execute --taker   # lift the ask immediately

Env vars:
    KALSHI_KEY_ID, KALSHI_KEY_PATH
"""

import argparse, os, pickle, re, time
from datetime import date, timedelta
import numpy as np
import pandas as pd

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from kalshi import KalshiClient
from utils import (
    get_json, ip_to_float, fetch_team_stats,
    extract_broadcasts, lookup_announcers,
    TRADE_DEADLINES, TICKER_SUFFIX_TO_WORD, ALLOWED_WORDS, TEST_WORDS, parse_wind,
)

# ── Config ─────────────────────────────────────────────────────────────────────

KALSHI_KEY_ID    = os.getenv('KALSHI_KEY_ID',  '')
KALSHI_KEY_PATH  = os.getenv('KALSHI_KEY_PATH', '~/.ssh/kalshi.pem')
MODEL_PATH       = os.path.join(_ROOT, 'models', 'kalshi_models.pkl')

# Cap per-bet spend at 50% of account balance.
# Quarter-Kelly fractions are applied against the live balance, so bet sizes
# scale naturally as the account grows or shrinks.
MAX_KELLY_FRAC   = 0.50

# Kalshi taker fee: 7% of *profit* (not of cost). Applied only on taker orders.
# On a YES bet at 40¢ that resolves YES: profit = 60¢, fee = 0.07 × 60¢ = 4.2¢.
# Maker orders (resting limit orders) are fee-free.
KALSHI_TAKER_FEE = 0.07

# Kalshi month abbreviations for constructing event tickers
MONTH_CODES = {
    1: 'JAN', 2: 'FEB',  3: 'MAR', 4: 'APR', 5: 'MAY',  6: 'JUN',
    7: 'JUL', 8: 'AUG',  9: 'SEP', 10: 'OCT', 11: 'NOV', 12: 'DEC',
}

# MLB abbreviation → Kalshi abbreviation (only entries that differ from MLB's standard)
MLB_TO_KALSHI = {
    'ARI': 'AZ',   # Diamondbacks: MLB uses ARI, Kalshi uses AZ
}

# ── MLB Stats API helpers (bet_today-specific) ────────────────────────────────

def fetch_today_games(game_date: date) -> list:
    """Fetch all MLB games for game_date with full hydration for feature building."""
    url  = (f'https://statsapi.mlb.com/api/v1/schedule?sportId=1'
            f'&date={game_date}&hydrate=team,venue,probablePitcher,broadcasts,'
            f'linescore,weather,game(seriesStatus)')
    data = get_json(url)
    if not data:
        return []
    games = []
    for day in data.get('dates', []):
        games.extend(day.get('games', []))
    return games


def fetch_pitcher_stats(person_id, season: int) -> dict:
    """Fetch season pitching stats for a pitcher — returns the raw stats dict.
    Uses strikeoutsPer9Inn key (from the people endpoint), unlike utils which
    computes k9 from raw strikeOuts. Both approaches are used in the codebase."""
    url  = (f'https://statsapi.mlb.com/api/v1/people/{person_id}/stats'
            f'?stats=season&season={season}&group=pitching&gameType=R')
    data = get_json(url)
    if not data:
        return {}
    for sg in data.get('stats', []):
        splits = sg.get('splits', [])
        if splits:
            return splits[0].get('stat', {})
    return {}


def days_to_deadline(game_date: date) -> int:
    """Days until the MLB trade deadline from game_date (0 if past deadline)."""
    deadline = TRADE_DEADLINES.get(game_date.year, date(game_date.year, 7, 31))
    return max(0, (deadline - game_date).days)


# ── Build feature rows from today's games ────────────────────────────────────

def build_feature_rows(games: list, game_date: date) -> pd.DataFrame:
    """Build one feature row per game. Returns empty DataFrame if no games.
    Stats are fetched as-of end_date (yesterday) so we never use same-day data.
    """
    rows = []
    # Stats "as of" the morning of game day — the API needs a closed interval
    end_date = str(game_date - timedelta(days=1))
    season   = game_date.year

    for g in games:
        status = g.get('status', {}).get('abstractGameState', '')
        if status == 'Final':
            continue

        home    = g['teams']['home']
        away    = g['teams']['away']
        home_id = home['team']['id']
        away_id = away['team']['id']
        game_pk = str(g.get('gamePk', ''))

        h_sp    = home.get('probablePitcher', {})
        a_sp    = away.get('probablePitcher', {})
        h_sp_id = h_sp.get('id', '')
        a_sp_id = a_sp.get('id', '')

        h_stats = fetch_pitcher_stats(h_sp_id, season) if h_sp_id else {}
        a_stats = fetch_pitcher_stats(a_sp_id, season) if a_sp_id else {}

        def sp_f(stats, key):
            v = stats.get(key)
            try:
                return float(v) if v not in (None, '', '--') else np.nan
            except (ValueError, TypeError):
                return np.nan

        h_sp_era = sp_f(h_stats, 'era')
        h_sp_ip  = ip_to_float(h_stats.get('inningsPitched', 0))
        h_sp_k9  = sp_f(h_stats, 'strikeoutsPer9Inn')
        h_sp_bb9 = sp_f(h_stats, 'walksPer9Inn')
        a_sp_era = sp_f(a_stats, 'era')
        a_sp_ip  = ip_to_float(a_stats.get('inningsPitched', 0))
        a_sp_k9  = sp_f(a_stats, 'strikeoutsPer9Inn')
        a_sp_bb9 = sp_f(a_stats, 'walksPer9Inn')

        h_rec   = home['leagueRecord']
        a_rec   = away['leagueRecord']
        h_wins  = int(h_rec.get('wins',   0))
        h_loss  = int(h_rec.get('losses', 0))
        a_wins  = int(a_rec.get('wins',   0))
        a_loss  = int(a_rec.get('losses', 0))

        venue_id  = g.get('venue', {}).get('id', '')
        game_type = g.get('gameType', 'R')
        day_night = g.get('dayNight', 'night')

        series_info     = g.get('seriesStatus', {})
        series_game_num = int(series_info.get('gameNumber', 1))

        weather    = g.get('weather', {})
        temp_f     = weather.get('temp', np.nan)
        wind_raw   = weather.get('wind', '')
        sky        = weather.get('condition', '')
        _ws, wind_dir = parse_wind(wind_raw)
        wind_speed    = float(_ws) if _ws is not None else np.nan

        # Season-to-date team stats through yesterday — two API calls per team
        h_ops, h_hr_g, h_r_g, h_era, h_whip, h_k9 = fetch_team_stats(home_id, season, end_date)
        a_ops, a_hr_g, a_r_g, a_era, a_whip, a_k9 = fetch_team_stats(away_id, season, end_date)

        broadcasts    = g.get('broadcasts', [])
        tv_nat, tv_hm = extract_broadcasts(broadcasts)
        ann1, ann2    = lookup_announcers(tv_hm, tv_nat, season)

        def _f(v):
            try:
                return float(v) if v not in (None, '', '--') else np.nan
            except (ValueError, TypeError):
                return np.nan

        row = {
            'game_pk':           game_pk,
            'home_team':         home['team'].get('abbreviation', ''),
            'away_team':         away['team'].get('abbreviation', ''),
            'game_time':         g.get('gameDate', ''),
            'series_game_num':   series_game_num,
            'attendance':        np.nan,
            'temp_f':            _f(temp_f),
            'wind_speed_mph':    _f(wind_speed),
            'home_wins':         h_wins,
            'home_losses':       h_loss,
            'away_wins':         a_wins,
            'away_losses':       a_loss,
            'home_sp_era':       h_sp_era,
            'home_sp_ip':        h_sp_ip,
            'home_sp_k9':        h_sp_k9,
            'home_sp_bb9':       h_sp_bb9,
            'away_sp_era':       a_sp_era,
            'away_sp_ip':        a_sp_ip,
            'away_sp_k9':        a_sp_k9,
            'away_sp_bb9':       a_sp_bb9,
            'days_to_trade_deadline': days_to_deadline(game_date),
            'home_ops':          _f(h_ops),
            'home_hr_g':         _f(h_hr_g),
            'home_r_g':          _f(h_r_g),
            'home_p_era':        _f(h_era),
            'home_p_whip':       _f(h_whip),
            'home_p_k9':         _f(h_k9),
            'away_ops':          _f(a_ops),
            'away_hr_g':         _f(a_hr_g),
            'away_r_g':          _f(a_r_g),
            'away_p_era':        _f(a_era),
            'away_p_whip':       _f(a_whip),
            'away_p_k9':         _f(a_k9),
            'venue_id':          str(venue_id),
            'game_type':         str(game_type),
            'day_night':         str(day_night),
            'sky':               str(sky),
            'wind_dir':          str(wind_dir),
            'tv_national':       str(tv_nat),
            'tv_home':           str(tv_hm),
            'announcer_1':       str(ann1),
            'announcer_2':       str(ann2),
            'home_sp_id':        str(h_sp_id),
            'away_sp_id':        str(a_sp_id),
            'home_team_id':      str(home_id),
            'away_team_id':      str(away_id),
        }
        rows.append(row)

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ── Prediction using kalshi_models.pkl ───────────────────────────────────────

def build_kalshi_feature_vector(row: dict, bundle: dict) -> np.ndarray:
    """
    Build a scaled feature vector for the kalshi LR model.
    Matches the feature construction in train_kalshi_model.py exactly —
    same column order, same one-hot encoding, same scaler.
    """
    num_features = bundle['num_features']
    scaler       = bundle['scaler']

    def _f(v):
        try:
            return float(v) if v not in (None, '', 'nan') and not (isinstance(v, float) and np.isnan(v)) else 0.0
        except (TypeError, ValueError):
            return 0.0

    x = [_f(row.get(c, 0)) for c in num_features]
    x_arr = np.array(x, dtype=float).reshape(1, -1)
    return scaler.transform(x_arr)


def predict_kalshi(df: pd.DataFrame, bundle: dict) -> pd.DataFrame:
    """
    Add a column p_{word_underscored} for each word using kalshi_models.pkl.
    Base-rate words get a constant; LR words get a per-game estimate.
    """
    df = df.copy()
    models = bundle['models']

    for word_spaced, mdl in models.items():
        col       = 'p_' + word_spaced.replace(' ', '_')
        base_rate = mdl['base_rate']

        if mdl['type'] == 'lr' and mdl.get('model') is not None:
            probs = []
            for _, row in df.iterrows():
                try:
                    xv   = build_kalshi_feature_vector(row.to_dict(), bundle)
                    p    = mdl['model'].predict_proba(xv)[0, 1]
                except Exception:
                    p = base_rate
                probs.append(p)
            df[col] = probs
        else:
            # Word has too few events to train reliably; use the historical average
            df[col] = base_rate

    return df


# ── Kalshi event ticker construction & market fetch ───────────────────────────

def mlb_abbr_to_kalshi(abbr: str) -> str:
    """Convert MLB team abbreviation to Kalshi's abbreviation (only ARI→AZ differs)."""
    return MLB_TO_KALSHI.get(abbr.upper(), abbr.upper())


def build_event_ticker(game_date: date, away_mlb: str, home_mlb: str) -> str:
    """Construct KXMLBMENTION-{YYMONDD}{AWAY}{HOME}.
    Example: 2026-05-07 NYY @ BOS → KXMLBMENTION-26MAY07NYYBOS
    """
    yy   = str(game_date.year)[2:]
    mon  = MONTH_CODES[game_date.month]
    dd   = f'{game_date.day:02d}'
    away = mlb_abbr_to_kalshi(away_mlb)
    home = mlb_abbr_to_kalshi(home_mlb)
    return f'KXMLBMENTION-{yy}{mon}{dd}{away}{home}'


def fetch_kalshi_markets_for_game(client: KalshiClient, event_ticker: str) -> dict:
    """
    Fetch all markets for the event and return {word_spaced: market_dict}.
    Uses the last segment of the ticker (e.g. 'WALK' from ...NYYBOS-WALK) to
    identify each word via TICKER_SUFFIX_TO_WORD. Skips NQE (not-qualified) markets.
    """
    try:
        markets = client.get_markets_for_event(event_ticker)
    except Exception as e:
        print(f'  WARN: could not fetch markets for {event_ticker}: {e}')
        return {}

    out = {}
    for mkt in markets:
        ticker = mkt.get('ticker', '')
        suffix = ticker.rsplit('-', 1)[-1] if '-' in ticker else ''
        word   = TICKER_SUFFIX_TO_WORD.get(suffix)
        if word:   # word is None for NQE (not-qualified) entries — skip those
            out[word] = mkt
    return out


# ── Kelly sizing ──────────────────────────────────────────────────────────────

def kelly_contracts(model_p: float, price_dollars: float, balance: float,
                    fee_rate: float = 0.0) -> int:
    """
    Quarter-Kelly contracts for a YES bet at price_dollars (0.0–1.0).

    Kelly formula:  f* = (b·p − q) / b
      where b = net return per dollar risked = (1 − price) / price
            p = model probability of YES
            q = 1 − p

    We use quarter-Kelly (×0.25) for safety because our LR model is small-N
    and base-rate words have zero model signal — full Kelly would be reckless.

    fee_rate is the Kalshi taker fee as a fraction of profit; it reduces the
    effective net return b and therefore Kelly. Pass 0.0 for maker orders.

    Returns floor(contracts), hard-capped at MAX_KELLY_FRAC × balance / price.
    """
    if price_dollars <= 0.0 or price_dollars >= 1.0:
        return 0
    # Adjust net return for fee on profit
    b = (1.0 - fee_rate) * (1.0 - price_dollars) / price_dollars
    q = 1.0 - model_p
    f = (b * model_p - q) / b
    f = max(0.0, f) * 0.25   # quarter-Kelly, floored at 0
    max_spend  = MAX_KELLY_FRAC * balance
    contracts  = int(f * balance / price_dollars)
    contracts  = min(contracts, int(max_spend / price_dollars))
    return max(contracts, 0)


# ── Main ──────────────────────────────────────────────────────────────────────


def _maker_yes_cents(mkt: dict) -> int:
    """
    YES-side maker price in cents: one cent inside the ask.
    Posting 1¢ below the ask puts us at the top of the buy book without
    being a market maker, so we capture the spread when someone lifts us.
    Maker orders carry no taker fee. Falls back to ask if no price data.
    """
    def _f(k):
        try:
            v = mkt.get(k)
            return float(v) if v not in (None, '', '0.0000') else None
        except (TypeError, ValueError):
            return None

    ask = _f('yes_ask_dollars')
    if ask is None:
        return None
    return max(1, min(99, round(ask * 100) - 1))




def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date',      default=str(date.today()))
    parser.add_argument('--min-edge',  type=float, default=0.08,
                        help='Minimum |model_p - market_p| to recommend a bet')
    parser.add_argument('--execute',   action='store_true')
    parser.add_argument('--taker',  action='store_true',
                        help='Lift the ask immediately instead of posting 1¢ inside as maker.')
    parser.add_argument('--no-kalshi', action='store_true',
                        help='Skip Kalshi lookup (offline test)')
    args = parser.parse_args()

    game_date = date.fromisoformat(args.date)
    print(f'\n=== bet_today.py  {game_date}  ({"LIVE" if args.execute else "DRY RUN"}) ===\n')

    print('Loading models...')
    with open(MODEL_PATH, 'rb') as fh:
        bundle = pickle.load(fh)
    words = list(bundle['models'].keys())
    lr_words = [w for w, m in bundle['models'].items() if m['type'] == 'lr']
    print(f'  {len(words)} words loaded ({len(lr_words)} with LR models: {lr_words})')

    print(f'\nFetching MLB schedule for {game_date}...')
    games = fetch_today_games(game_date)
    print(f'  {len(games)} games found')
    if not games:
        print('No games today. Exiting.')
        return

    print('\nBuilding pre-game feature rows...')
    df = build_feature_rows(games, game_date)
    if df.empty:
        print('No feature rows built. Exiting.')
        return
    print(f'  {len(df)} game rows ready')

    print('\nRunning model predictions...')
    df = predict_kalshi(df, bundle)

    print('\nGame lineup:')
    for _, row in df.iterrows():
        nat = row.get('tv_national', '')
        print(f'  {row["away_team"]} @ {row["home_team"]}'
              f'  venue={row["venue_id"]}'
              f'  {"📺 " + nat if nat else "local only"}')

    recs = []

    if not args.no_kalshi:
        if not KALSHI_KEY_ID:
            print('\nWARN: KALSHI_KEY_ID not set — skipping Kalshi lookup.')
            args.no_kalshi = True

    if not args.no_kalshi:
        print('\nConnecting to Kalshi...')
        client  = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH)
        balance = client.get_balance()
        print(f'  Balance: ${balance:.2f}')

        for _, game_row in df.iterrows():
            away    = game_row['away_team']
            home    = game_row['home_team']
            event_t = build_event_ticker(game_date, away, home)
            print(f'\n  Fetching Kalshi: {event_t}')
            word_markets = fetch_kalshi_markets_for_game(client, event_t)
            time.sleep(0.3)

            if not word_markets:
                print(f'  No markets found for {event_t} — game may not be listed on Kalshi')
                continue

            print(f'  {len(word_markets)} word markets found')

            for word, mkt in word_markets.items():
                is_test = word in TEST_WORDS
                if word not in ALLOWED_WORDS and not is_test:
                    continue
                col     = 'p_' + word.replace(' ', '_')
                model_p = float(game_row.get(col, np.nan))
                if np.isnan(model_p):
                    continue

                market_p = KalshiClient.implied_prob(mkt)
                if market_p is None:
                    continue

                if model_p > market_p:
                    side            = 'yes'
                    edge            = model_p - market_p
                    taker_ask_d     = float(mkt.get('yes_ask_dollars') or market_p)
                    price_d         = taker_ask_d
                    taker_yes_cents = round(taker_ask_d * 100)
                else:
                    side            = 'no'
                    edge            = (1 - model_p) - (1 - market_p)
                    taker_ask_no_d  = float(mkt.get('no_ask_dollars') or (1 - market_p))
                    price_d         = taker_ask_no_d
                    # YES-equivalent price for NO taker: what YES price does this NO ask imply?
                    taker_yes_cents = round((1 - taker_ask_no_d) * 100)

                if edge < args.min_edge:
                    continue

                effective_model_p = model_p if side == 'yes' else 1 - model_p
                fee = KALSHI_TAKER_FEE if args.taker else 0.0
                n = kelly_contracts(effective_model_p, price_d, balance, fee_rate=fee)
                if is_test:
                    n = 1  # test words: 1 contract to accumulate data, no real sizing
                if n < 1:
                    continue

                # Maker price: 1¢ below the ask so we rest on the book without taker fees
                mc = _maker_yes_cents(mkt)
                if mc is not None:
                    maker_yes_cents = mc
                    maker_price_d   = (maker_yes_cents / 100 if side == 'yes'
                                       else (100 - maker_yes_cents) / 100)
                else:
                    maker_yes_cents = taker_yes_cents
                    maker_price_d   = price_d

                recs.append({
                    'game':            f'{away}@{home}',
                    'game_time':       game_row.get('game_time', ''),
                    'word':            word,
                    'side':            side,
                    'model_p':         model_p,
                    'mkt_p':           market_p,
                    'edge':            edge,
                    'ticker':          mkt['ticker'],
                    'price_d':         price_d,
                    'yes_cents':       taker_yes_cents,
                    'maker_yes_cents': maker_yes_cents,
                    'maker_price_d':   maker_price_d,
                    'n':               n,
                    'cost':            round(n * price_d, 2),
                    'maker_cost':      round(n * maker_price_d, 2),
                })

    # Model probabilities table
    print('\n── Model probabilities ──────────────────────────────────────────────')
    prob_cols = sorted(c for c in df.columns if c.startswith('p_'))
    if prob_cols:
        header = f'  {"Game":<18}' + ''.join(f' {c[2:]:>11}' for c in prob_cols)
        print(header)
        for _, row in df.iterrows():
            game_str = f'{row["away_team"]}@{row["home_team"]}'
            vals     = ''.join(f' {row[c]:>11.1%}' for c in prob_cols)
            print(f'  {game_str:<18}{vals}')

    if not recs:
        print('\nNo bets meet the edge threshold today.')
        return

    print(f'\n── Bet recommendations (edge ≥ {args.min_edge:.0%}) ────────────────────')
    print(f'  {"Game":<20} {"Word":<14} {"Side":<4} {"Model":>6} {"Mkt":>6}'
          f' {"Edge":>6} {"Qty":>4} {"Taker$":>7} {"Maker$":>7}  Ticker')
    for r in sorted(recs, key=lambda x: -x['edge']):
        print(f'  {r["game"]:<20} {r["word"]:<14} {r["side"]:<4}'
              f' {r["model_p"]:>6.1%} {r["mkt_p"]:>6.1%} {r["edge"]:>6.1%}'
              f' {r["n"]:>4} {r["cost"]:>7.2f} {r["maker_cost"]:>7.2f}  {r["ticker"]}')

    total_taker = sum(r['cost'] for r in recs)
    total_maker = sum(r['maker_cost'] for r in recs)
    print(f'\n  Total spend: ${total_taker:.2f} taker / ${total_maker:.2f} maker')

    if args.execute:
        use_maker  = not args.taker
        mode_label = 'taker (lifting ask, fees apply)' if args.taker else 'maker (1¢ inside ask, no fees)'
        print(f'\nPlacing orders as {mode_label}...')

        for r in recs:
            yes_cents = r['maker_yes_cents'] if use_maker else r['yes_cents']
            result    = client.place_order(
                ticker          = r['ticker'],
                side            = r['side'],
                yes_price_cents = yes_cents,
                count           = r['n'],
                dry_run         = False,
            )
            order  = result.get('order', {})
            status = order.get('status', '')
            fee    = order.get('taker_fees_dollars', '0')
            fill   = float(order.get('fill_count_fp', 0))

            price_label = f'{yes_cents}¢ YES' if r['side'] == 'yes' else f'{100-yes_cents}¢ NO'
            print(f'  {r["ticker"]}  {r["side"]} x{r["n"]} @ {price_label}'
                  f'  status={status}  filled={fill:.0f}  fee=${fee}')
    else:
        print(f'\n(Use --execute to place as maker at the bid, or --execute --taker to lift the ask immediately.)')


if __name__ == '__main__':
    main()
