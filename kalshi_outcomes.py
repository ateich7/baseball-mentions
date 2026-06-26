"""
kalshi_outcomes.py — builds kalshi_outcomes.csv, the ground-truth training set.

For each settled KXMLBMENTION event, the script:
  1. Downloads all 16 word markets from Kalshi
  2. Matches the event to an MLB game via the Stats API
  3. Builds the same feature vector used in bet_today.py / monitor.py
  4. Records yes/no outcomes for each word and the open/close prices

Run daily (via retrain.py) to keep the training set current.
Usage:
    KALSHI_KEY_ID=... KALSHI_KEY_PATH=~/.ssh/kalshi.pem python kalshi_outcomes.py
"""

import csv, os, re, sys, time
from datetime import date, timedelta

from kalshi import KalshiClient
from broadcast_crews import CREW
from utils import (
    get_json, ip_to_float, pitcher_stats_cols,
    fetch_pitcher_season_stats, fetch_team_stats,
    extract_broadcasts, lookup_announcers,
    TRADE_DEADLINES, parse_wind,
)
# ── Output ────────────────────────────────────────────────────────────────────

OUTPUT = 'kalshi_outcomes.csv'

# ── Word / market mapping ─────────────────────────────────────────────────────

WORD_MAP = {
    'Bullpen':                  'bullpen',
    'Pinch Hitter / Pinch Hit': 'pinch_hit',
    'Pinch Hit':                'pinch_hit',
    'Pinch Hitter':             'pinch_hit',
    'MVP':                      'mvp',
    'Perfect Game':             'perfect_game',
    'Pitch Clock':              'pitch_clock',
    'Grand Slam':               'grand_slam',
    'Extra Inning':             'extra_inning',
    'Trade / Traded':           'trade',
    'Bases Loaded':             'bases_loaded',
    'Triple':                   'triple',
    'What a Catch':             'what_a_catch',
    'Walk Off':                 'walk_off',
    'Bunt / Bunted':            'bunt',
    'Bunt':                     'bunt',
    'Error':                    'error',
    'Wild Pitch':               'wild_pitch',
    'Double Play':              'double_play',
}

# Ticker suffix → canonical column name (more reliable than string matching)
TICKER_SUFFIX_MAP = {
    'BULL': 'bullpen',
    'PINC': 'pinch_hit',
    'MVP':  'mvp',
    'PERF': 'perfect_game',
    'PITC': 'pitch_clock',
    'GRAN': 'grand_slam',
    'EXTR': 'extra_inning',
    'TRAD': 'trade',
    'BASE': 'bases_loaded',
    'TRIP': 'triple',
    'WHAT': 'what_a_catch',
    'WALK': 'walk_off',
    'BUNT': 'bunt',
    'ERRO': 'error',
    'WILD': 'wild_pitch',
    'DOUB': 'double_play',
    'NQE':  None,  # "Event does not qualify" — skip
}

# All 16 canonical column names (underscored)
WORD_COLS = list(dict.fromkeys(WORD_MAP.values()))  # deduplicated

# ── Feature columns (must match train_models.py) ──────────────────────────────

NUM_COLS = [
    'series_game_num', 'attendance', 'temp_f', 'wind_speed_mph',
    'home_wins', 'home_losses', 'away_wins', 'away_losses',
    'home_sp_era', 'home_sp_ip', 'home_sp_k9', 'home_sp_bb9',
    'away_sp_era', 'away_sp_ip', 'away_sp_k9', 'away_sp_bb9',
    'days_to_trade_deadline',
    'home_ops', 'home_hr_g', 'home_r_g', 'home_p_era', 'home_p_whip', 'home_p_k9',
    'away_ops', 'away_hr_g', 'away_r_g', 'away_p_era', 'away_p_whip', 'away_p_k9',
]

CAT_COLS = [
    'venue_id', 'game_type', 'day_night', 'sky', 'wind_dir',
    'tv_national', 'tv_home', 'announcer_1', 'announcer_2',
    'home_sp_id', 'away_sp_id', 'home_team_id', 'away_team_id',
]

# ── Kalshi abbreviation → MLB Stats API abbreviation ─────────────────────────
# The Kalshi ticker uses 2–3 char uppercase abbreviations embedded in the game
# code.  We match against the 'abbreviation' field returned by the Stats API
# schedule endpoint.

KALSHI_TO_MLB = {
    'LAD': 'LAD',
    'STL': 'STL',
    'KC':  'KC',
    'SEA': 'SEA',
    'CIN': 'CIN',
    'PIT': 'PIT',
    'DET': 'DET',
    'ATL': 'ATL',
    'TB':  'TB',
    'CLE': 'CLE',
    'TEX': 'TEX',
    'NYY': 'NYY',
    'NYM': 'NYM',
    'BOS': 'BOS',
    'HOU': 'HOU',
    'SF':  'SF',
    'SD':  'SD',
    'MIL': 'MIL',
    'MIN': 'MIN',
    'CHC': 'CHC',
    'CWS': 'CWS',
    'PHI': 'PHI',
    'TOR': 'TOR',
    'BAL': 'BAL',
    'COL': 'COL',
    'ARI': 'ARI',
    'MIA': 'MIA',
    'WSH': 'WSH',
    'LAA': 'LAA',
    'ATH': 'ATH',   # Sacramento Athletics (post-Oakland move)
    'OAK': 'ATH',   # legacy Oakland abbreviation
    'AZ':  'ARI',   # Kalshi uses AZ for Diamondbacks
}


# ═══════════════════════════════════════════════════════════════════════════════
# MLB Stats API helpers (self-contained, adapted from enrich_data.py /
# add_team_stats.py / add_broadcasters.py)
# ═══════════════════════════════════════════════════════════════════════════════

MONTH_MAP = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,  'MAY': 5,  'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}

# KXMLBMENTION-26MAY02LADSTL
# suffix: YYMONDDAWAYOME  where away/home are 2–3 char abbreviations
_TICKER_RE = re.compile(
    r'KXMLBMENTION-(\d{2})([A-Z]{3})(\d{2})([A-Z]{4,6})$'
)


def parse_ticker(ticker: str):
    """
    Parse a KXMLBMENTION event ticker into (game_date, away_abbr, home_abbr).
    Returns (None, None, None) if format does not match.
    Handles 2-char abbreviations (KC, TB, SF, AZ, SD) by trying all splits.
    """
    m = _TICKER_RE.match(ticker)
    if not m:
        return None, None, None
    yy, mon, dd, teams_str = m.groups()
    month = MONTH_MAP.get(mon)
    if not month:
        return None, None, None
    try:
        game_date = date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None, None, None

    # Try splits: 2+2, 2+3, 3+2, 3+3
    known = set(KALSHI_TO_MLB.keys())
    away_k = home_k = None
    for split in (2, 3):
        a, h = teams_str[:split], teams_str[split:]
        if len(h) in (2, 3) and a in known and h in known:
            away_k, home_k = a, h
            break

    if away_k is None:
        return None, None, None
    return game_date, away_k, home_k


# ═══════════════════════════════════════════════════════════════════════════════
# MLB schedule lookup
# ═══════════════════════════════════════════════════════════════════════════════

def find_game_pk(game_date: date, away_kalshi: str, home_kalshi: str) -> tuple:
    """
    Query the MLB Stats API schedule for game_date and return
    (game_pk, home_team_id, away_team_id, full_game_dict) that matches
    the Kalshi abbreviations.  Returns (None, None, None, None) on miss.
    """
    away_mlb = KALSHI_TO_MLB.get(away_kalshi, away_kalshi)
    home_mlb = KALSHI_TO_MLB.get(home_kalshi, home_kalshi)

    date_str = game_date.isoformat()
    url = (f'https://statsapi.mlb.com/api/v1/schedule?sportId=1'
           f'&date={date_str}'
           f'&hydrate=team,venue,weather,broadcasts,linescore')
    data = get_json(url)
    time.sleep(0.15)
    if not data:
        return None, None, None, None

    for day in data.get('dates', []):
        for g in day.get('games', []):
            h_abbr = g.get('teams', {}).get('home', {}).get('team', {}).get('abbreviation', '')
            a_abbr = g.get('teams', {}).get('away', {}).get('team', {}).get('abbreviation', '')
            if h_abbr == home_mlb and a_abbr == away_mlb:
                pk   = str(g.get('gamePk', ''))
                hid  = g['teams']['home']['team']['id']
                aid  = g['teams']['away']['team']['id']
                return pk, hid, aid, g

    # If no exact match, try a case-insensitive partial match as fallback
    for day in data.get('dates', []):
        for g in day.get('games', []):
            h_abbr = g.get('teams', {}).get('home', {}).get('team', {}).get('abbreviation', '').upper()
            a_abbr = g.get('teams', {}).get('away', {}).get('team', {}).get('abbreviation', '').upper()
            if h_abbr == home_mlb.upper() and a_abbr == away_mlb.upper():
                pk  = str(g.get('gamePk', ''))
                hid = g['teams']['home']['team']['id']
                aid = g['teams']['away']['team']['id']
                return pk, hid, aid, g

    return None, None, None, None


def extract_game_features(g: dict, game_date: date,
                          home_team_abbr: str, away_team_abbr: str) -> dict:
    """
    Pull schedule-level features from the hydrated game dict returned by
    find_game_pk.  Returns a flat dict keyed by feature column name.
    """
    out = {c: '' for c in NUM_COLS + CAT_COLS + ['is_national']}

    season = game_date.year
    deadline = TRADE_DEADLINES.get(season, date(season, 7, 31))
    out['days_to_trade_deadline'] = (deadline - game_date).days

    out['game_type']      = g.get('gameType', '')
    out['series_game_num'] = g.get('seriesGameNumber', '')
    out['day_night']      = g.get('dayNight', '')

    att = g.get('attendance')
    if att:
        out['attendance'] = att

    v = g.get('venue', {})
    out['venue_id'] = v.get('id', '')

    w = g.get('weather', {})
    out['temp_f'] = w.get('temp', '')
    out['sky']    = w.get('condition', '')
    wind = w.get('wind', '')
    if wind:
        m = re.search(r'(\d+)\s*mph', wind, re.I)
        if m:
            out['wind_speed_mph'] = int(m.group(1))
        m = re.search(r'mph[,\s]+(.+)', wind, re.I)
        if m:
            out['wind_dir'] = m.group(1).strip()

    for side in ('home', 'away'):
        rec = g.get('teams', {}).get(side, {}).get('leagueRecord', {})
        out[f'{side}_wins']   = rec.get('wins', '')
        out[f'{side}_losses'] = rec.get('losses', '')

    out['home_team_id'] = g.get('teams', {}).get('home', {}).get('team', {}).get('id', '')
    out['away_team_id'] = g.get('teams', {}).get('away', {}).get('team', {}).get('id', '')

    # Broadcasts
    broadcasts = g.get('broadcasts', [])
    tv_nat, tv_hm = extract_broadcasts(broadcasts)
    out['tv_national'] = tv_nat
    out['tv_home']     = tv_hm
    out['is_national'] = 1 if tv_nat else 0

    ann1, ann2 = lookup_announcers(tv_hm, tv_nat, season)
    out['announcer_1'] = ann1
    out['announcer_2'] = ann2

    return out


def enrich_from_boxscore(game_pk: str, season: int,
                         pitcher_cache: dict) -> dict:
    """
    Fetch the boxscore for game_pk and return a partial dict with:
      home_sp_id, away_sp_id  (for CAT_COLS)
      attendance fallback, wind fallback (when schedule didn't have them)
    Also populates pitcher_cache[(pid, season)] = None to mark for later fetch.
    """
    out = {}
    url = f'https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore'
    box = get_json(url)
    time.sleep(0.15)
    if not box:
        return out

    for side in ('home', 'away'):
        pitchers = box.get('teams', {}).get(side, {}).get('pitchers', [])
        if not pitchers:
            continue
        sid     = pitchers[0]
        players = box.get('teams', {}).get(side, {}).get('players', {})
        p       = players.get(f'ID{sid}', {})
        out[f'{side}_sp_id'] = sid
        if (sid, season) not in pitcher_cache:
            pitcher_cache[(sid, season)] = None   # mark for fetch

    # attendance / weather fallback from info[]
    info = {x.get('label', '').lower(): x.get('value', '')
            for x in box.get('info', []) if x.get('label')}
    att_s = info.get('att', '')
    if att_s:
        out['_box_attendance'] = re.sub(r'[^\d]', '', att_s)
    wx = info.get('weather', '')
    if wx:
        out['_box_weather'] = wx
    wd = info.get('wind', '')
    if wd:
        out['_box_wind'] = wd

    return out


# ═══════════════════════════════════════════════════════════════════════════════
# Market parsing helpers
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_subtitle(subtitle: str) -> str:
    """Normalise a Kalshi market sub_title to lowercase for word matching."""
    return subtitle.strip().lower()


def match_word_by_ticker(market_ticker: str):
    """Match via the 4-char suffix at the end of the market ticker."""
    suffix = market_ticker.rsplit('-', 1)[-1] if '-' in market_ticker else ''
    return TICKER_SUFFIX_MAP.get(suffix, '')   # '' = unknown, None = explicit skip


def match_word(market_title: str, market_subtitle: str):
    """
    Match a Kalshi market to one of the 16 words via yes_sub_title / title.
    Returns canonical column name or None.  Skips empty strings to avoid
    false-positive '' matches.
    """
    candidates = [market_subtitle, market_title]
    for text in candidates:
        if not text:
            continue
        tl = text.strip().lower()
        for raw_word, col in WORD_MAP.items():
            if raw_word.lower() == tl or raw_word.lower() in tl:
                return col
    return None


def result_to_int(result: str):
    """Convert Kalshi result string ('yes'/'no') to 1/0, or '' if missing."""
    r = str(result).strip().lower()
    if r == 'yes':
        return 1
    if r == 'no':
        return 0
    return ''


def cents_to_dollars(val) -> str:
    """Convert Kalshi cents integer to dollar string, or '' if absent."""
    if val is None or val == '':
        return ''
    try:
        return str(round(int(val) / 100, 2))
    except (TypeError, ValueError):
        return ''


# ═══════════════════════════════════════════════════════════════════════════════
# Main pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    key_id   = os.environ.get('KALSHI_KEY_ID', '')
    key_path = os.environ.get('KALSHI_KEY_PATH', '')
    if not key_id or not key_path:
        print('ERROR: set KALSHI_KEY_ID and KALSHI_KEY_PATH env vars.', file=sys.stderr)
        sys.exit(1)

    kalshi = KalshiClient(key_id=key_id, private_key_path=key_path)

    # ── Step 1: download all settled KXMLBMENTION events ─────────────────────
    print('Fetching settled KXMLBMENTION events from Kalshi...')
    events = kalshi.get_events('KXMLBMENTION', status='settled')
    print(f'  Got {len(events)} settled events')
    time.sleep(0.3)

    if not events:
        print('No events found. Exiting.')
        sys.exit(0)

    # ── Step 2: for each event, fetch its 16 markets ──────────────────────────
    print('\nFetching markets for each event...')
    event_markets = {}   # event_ticker → list[market dict]

    for i, ev in enumerate(events):
        ticker = ev.get('event_ticker', '')
        if not ticker:
            continue
        markets = kalshi.get_markets_for_event(ticker)
        event_markets[ticker] = markets
        print(f'  [{i+1}/{len(events)}] {ticker}  ({len(markets)} markets)', flush=True)
        time.sleep(0.3)

    print(f'\nMarket data collected for {len(event_markets)} events')

    # ── Step 3: per-event — parse ticker, find game, build features ───────────
    print('\nBuilding features and outcomes per event...\n')

    pitcher_cache = {}   # (pitcher_id, season) → stats dict | None
    team_stats_cache = {}   # (team_id, end_date_str) → (ops, hr_g, r_g, era, whip, p_k9)

    rows = []

    for idx, (event_ticker, markets) in enumerate(event_markets.items()):
        print(f'[{idx+1}/{len(event_markets)}] {event_ticker}', end=' ', flush=True)

        # Parse ticker
        game_date, away_k, home_k = parse_ticker(event_ticker)
        if game_date is None:
            print('  SKIP: could not parse ticker')
            continue

        date_raw    = game_date.isoformat()
        season      = game_date.year
        home_abbr   = KALSHI_TO_MLB.get(home_k, home_k)
        away_abbr   = KALSHI_TO_MLB.get(away_k, away_k)

        # ── Collect word outcomes and prices from markets ─────────────────────
        word_results = {col: '' for col in WORD_COLS}
        open_prices  = {col: '' for col in WORD_COLS}
        close_prices = {col: '' for col in WORD_COLS}

        for mkt in markets:
            mkt_ticker = mkt.get('ticker', '') or ''
            # Primary: ticker-suffix lookup (most reliable)
            col = match_word_by_ticker(mkt_ticker)
            if col == '':
                # Fallback: yes_sub_title / title string match
                sub   = mkt.get('yes_sub_title', '') or mkt.get('sub_title', '') or ''
                title = mkt.get('title', '') or ''
                col   = match_word(title, sub)
            if not col:
                continue   # None (explicit skip like NQE) or unrecognised

            # Outcome
            result = mkt.get('result', '')
            word_results[col] = result_to_int(result)

            # Prices: use dollar-string fields (not integer cent fields)
            # open_price: previous_yes_ask_dollars (last pre-settle price proxy)
            # close_price: yes_ask_dollars (final ask)
            open_prices[col]  = mkt.get('previous_yes_ask_dollars', '') or ''
            close_prices[col] = mkt.get('yes_ask_dollars', '') or ''

        # ── Find game on MLB Stats API ────────────────────────────────────────
        game_pk, home_team_id, away_team_id, game_dict = find_game_pk(
            game_date, away_k, home_k
        )

        if game_pk is None:
            print(f'  WARN: no MLB game found for {game_date} {away_k}@{home_k}')

        # ── Schedule-level features ───────────────────────────────────────────
        if game_dict is not None:
            features = extract_game_features(game_dict, game_date, home_abbr, away_abbr)
        else:
            features = {c: '' for c in NUM_COLS + CAT_COLS + ['is_national']}
            features['days_to_trade_deadline'] = (
                TRADE_DEADLINES.get(season, date(season, 7, 31)) - game_date
            ).days
            features['home_team_id'] = home_team_id or ''
            features['away_team_id'] = away_team_id or ''

        # ── Boxscore: starter IDs + attendance/weather fallback ───────────────
        if game_pk:
            box_extras = enrich_from_boxscore(game_pk, season, pitcher_cache)
            features['home_sp_id'] = box_extras.get('home_sp_id', '')
            features['away_sp_id'] = box_extras.get('away_sp_id', '')

            # Attendance fallback
            if not features.get('attendance') and '_box_attendance' in box_extras:
                features['attendance'] = box_extras['_box_attendance']

            # Weather fallback from boxscore info[]
            if not features.get('temp_f') and '_box_weather' in box_extras:
                wx = box_extras['_box_weather']
                m  = re.search(r'(\d+)\s*degrees', wx, re.I)
                if m:
                    features['temp_f'] = int(m.group(1))
                m = re.search(r'degrees[,\s]+(.+?)\.?\s*$', wx, re.I)
                if m:
                    features['sky'] = m.group(1).strip()
            if not features.get('wind_speed_mph') and '_box_wind' in box_extras:
                wd = box_extras['_box_wind']
                m  = re.search(r'(\d+)\s*mph', wd, re.I)
                if m:
                    features['wind_speed_mph'] = int(m.group(1))
                m = re.search(r'mph[,\s]+(.+)', wd, re.I)
                if m:
                    features['wind_dir'] = m.group(1).strip()

        # Build output row (metadata first, then features, then labels, then prices)
        row = {
            'event_ticker':    event_ticker,
            'game_pk':         game_pk or '',
            'date_raw':        date_raw,
            'home_team_abbr':  home_abbr,
            'away_team_abbr':  away_abbr,
        }
        row.update(features)
        row.update(word_results)
        row.update({f'open_{col}':  open_prices[col]  for col in WORD_COLS})
        row.update({f'close_{col}': close_prices[col] for col in WORD_COLS})

        rows.append(row)
        print('✓', flush=True)

    print(f'\n{len(rows)} game rows assembled.')

    # ── Step 4: pitcher season stats ─────────────────────────────────────────
    unique_pitchers = [(pid, ssn) for (pid, ssn), v in pitcher_cache.items() if v is None]
    print(f'\nFetching pitcher season stats for {len(unique_pitchers)} pitcher-seasons...')

    for j, (pid, ssn) in enumerate(unique_pitchers):
        pitcher_cache[(pid, ssn)] = fetch_pitcher_season_stats(pid, ssn)
        if (j + 1) % 20 == 0 or j + 1 == len(unique_pitchers):
            print(f'  {j+1}/{len(unique_pitchers)}', flush=True)

    # Attach pitcher stats to rows
    for row in rows:
        try:
            ssn = date.fromisoformat(row['date_raw']).year
        except Exception:
            ssn = 2025
        for side in ('home', 'away'):
            pid = row.get(f'{side}_sp_id', '')
            if pid:
                stats = pitcher_cache.get((pid, ssn)) or {}
                era, ip, k9, bb9 = pitcher_stats_cols(stats)
                row[f'{side}_sp_era'] = era
                row[f'{side}_sp_ip']  = ip
                row[f'{side}_sp_k9']  = k9
                row[f'{side}_sp_bb9'] = bb9

    # ── Step 5: team season stats (season-to-date, cached) ───────────────────
    work = []   # (row_index, home_id, away_id, season, end_date_str)
    for i, row in enumerate(rows):
        hid = row.get('home_team_id', '')
        aid = row.get('away_team_id', '')
        if not hid or not aid:
            continue
        try:
            gd  = date.fromisoformat(row['date_raw'])
            ssn = gd.year
            end = str(gd - timedelta(days=1))
        except Exception:
            continue
        work.append((i, int(hid), int(aid), ssn, end))

    unique_team_fetches = {
        (tid, end, ssn)
        for _, hid, aid, ssn, end in work
        for tid in (hid, aid)
    }
    print(f'\nFetching team stats for {len(unique_team_fetches)} unique (team, date) pairs...')

    done = 0
    for team_id, end_date, ssn in sorted(unique_team_fetches):
        key = (team_id, end_date)
        if key not in team_stats_cache:
            team_stats_cache[key] = fetch_team_stats(team_id, ssn, end_date)
            done += 1
            if done % 20 == 0 or done == len(unique_team_fetches):
                print(f'  {done}/{len(unique_team_fetches)}', flush=True)

    # Attach team stats to rows
    for i, hid, aid, ssn, end in work:
        row = rows[i]
        h = team_stats_cache.get((hid, end), ('',) * 6)
        a = team_stats_cache.get((aid, end), ('',) * 6)
        h_ops, h_hr_g, h_r_g, h_era, h_whip, h_k9 = h
        a_ops, a_hr_g, a_r_g, a_era, a_whip, a_k9 = a
        row.update({
            'home_ops':    h_ops,  'home_hr_g': h_hr_g, 'home_r_g':    h_r_g,
            'home_p_era':  h_era,  'home_p_whip': h_whip, 'home_p_k9': h_k9,
            'away_ops':    a_ops,  'away_hr_g': a_hr_g, 'away_r_g':    a_r_g,
            'away_p_era':  a_era,  'away_p_whip': a_whip, 'away_p_k9': a_k9,
        })

    # ── Step 6: write CSV ─────────────────────────────────────────────────────
    metadata_cols  = ['event_ticker', 'game_pk', 'date_raw', 'home_team_abbr', 'away_team_abbr']
    open_cols      = [f'open_{col}'  for col in WORD_COLS]
    close_cols     = [f'close_{col}' for col in WORD_COLS]
    all_cols       = (
        metadata_cols
        + NUM_COLS
        + CAT_COLS
        + ['is_national']
        + WORD_COLS
        + open_cols
        + close_cols
    )

    with open(OUTPUT, 'w', newline='') as fout:
        writer = csv.DictWriter(fout, fieldnames=all_cols, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

    print(f'\nWrote {len(rows)} rows → {OUTPUT}')

    # ── Quick QA ──────────────────────────────────────────────────────────────
    if rows:
        filled = lambda col: sum(1 for r in rows if r.get(col) not in ('', None))
        n = len(rows)
        print('\nFill rates (key columns):')
        for col in ['game_pk', 'venue_id', 'temp_f', 'home_sp_id',
                    'home_ops', 'home_p_era', 'tv_national', 'tv_home',
                    'announcer_1'] + WORD_COLS[:4]:
            pct = 100 * filled(col) // n if n else 0
            print(f'  {col:<28}: {filled(col)}/{n} ({pct}%)')

        # Count how many events have all 16 words resolved
        all_resolved = sum(
            1 for r in rows
            if all(r.get(col) in (0, 1, '0', '1') for col in WORD_COLS)
        )
        print(f'\n  Events with all 16 words resolved: {all_resolved}/{n}')


if __name__ == '__main__':
    main()
