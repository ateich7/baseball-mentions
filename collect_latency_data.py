"""
collect_latency_data.py — MLB API → Kalshi market repricing lag measurement.

Strategy context
----------------
TV viewers (and Kalshi traders) are 30-60 seconds behind reality due to
broadcast delay. The MLB Stats API receives data from Statcast sensors —
essentially real-time. When a play occurs we detect it before TV viewers
know, giving a window to trade stale Kalshi prices.

What this script does
---------------------
Runs a single loop every POLL_INTERVAL seconds for each active game:
  1. Polls /api/v1.1/game/{gamePk}/feed/live — detects new play events
  2. Fetches prices for ALL 12 Kalshi markets for that game
  3. Writes both to disk immediately (no in-memory accumulation)

This produces two parallel time series that analysis can correlate:
  - events: when did each play happen (detected_ts)
  - prices: what were all 12 market prices at each point in time

Analysis (analyze_latency.py) aligns events to the price time series by
timestamp to measure: how long after event detection did each market reprice,
and by how much.

Output
------
  data/latency_events.jsonl  — one JSON per detected event
  data/latency_prices.csv    — all-market price snapshot every POLL_INTERVAL s

Usage (server only — Kalshi credentials not local):
  KALSHI_KEY_ID=5da099c4-b40e-4ce0-a9a0-9f22fad881e5 \\
  KALSHI_KEY_PATH=/root/baseball-mentions/kalshi_key.txt \\
  /root/baseball-env/bin/python collect_latency_data.py

Analysis (next session):
  /root/baseball-env/bin/python analyze_latency.py
"""

import csv, json, os, re, sys, time
from datetime import date, datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Tuple

from kalshi import KalshiClient
from utils import get_json, TICKER_SUFFIX_TO_WORD

# ── Config ────────────────────────────────────────────────────────────────────

KALSHI_KEY_ID    = os.getenv('KALSHI_KEY_ID',    '')
KALSHI_KEY_PATH  = os.getenv('KALSHI_KEY_PATH',  '/root/baseball-mentions/kalshi_key.txt')
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN',   '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

POLL_INTERVAL      = 10    # seconds per loop — MLB live feed + all market prices
HEARTBEAT_INTERVAL = 1800  # 30 minutes

DATA_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
EVENTS_FILE = os.path.join(DATA_DIR, 'latency_events.jsonl')
PRICES_FILE = os.path.join(DATA_DIR, 'latency_prices.csv')

ET = timezone(timedelta(hours=-4))  # EDT; change to -5 after Nov DST

MONTH_CODES = {
    1: 'JAN', 2: 'FEB',  3: 'MAR', 4: 'APR',  5: 'MAY',  6: 'JUN',
    7: 'JUL', 8: 'AUG',  9: 'SEP', 10: 'OCT', 11: 'NOV', 12: 'DEC',
}
MONTH_TO_NUM = {v: k for k, v in MONTH_CODES.items()}

MLB_TO_KALSHI = {'ARI': 'AZ'}
KALSHI_TO_MLB = {'AZ': 'ARI', 'OAK': 'ATH'}   # non-identity overrides
KALSHI_2CHAR  = frozenset({'KC', 'TB', 'SF', 'AZ', 'SD'})

# ── Kalshi word mappings (where known) ───────────────────────────────────────
# ALL events are recorded regardless of mapping. These dicts provide the
# optional Kalshi word label when one exists — the model learns which events
# actually matter for pricing, not us.

# allPlays[].result.event → Kalshi word
RESULT_WORD_MAP = {
    'Wild Pitch':            'wild pitch',
    'Triple':                'triple',
    'Double Play':           'double play',
    'Grounded Into DP':      'double play',
    'Strikeout Double Play': 'double play',
    'Bunt Groundout':        'bunt',
    'Bunt Pop Out':          'bunt',
    'Sacrifice Bunt':        'bunt',
    'Bunt Lineout':          'bunt',
    'Field Error':           'error',
    'Fielding Error':        'error',
    'Pitching Substitution': 'bullpen',
}

# allPlays[].playEvents[].details.eventType → Kalshi word
ACTION_WORD_MAP = {
    'wild_pitch':   'wild pitch',
    'passed_ball':  'wild pitch',   # similar effect; announcers may say "wild pitch" loosely
}

# Action eventTypes that are metadata, not actual plays — skip these
ACTION_SKIP = {'game_advisory', 'game_status'}

# ── Utilities ─────────────────────────────────────────────────────────────────

def ts_now() -> float:
    return time.time()


def log(msg: str) -> None:
    t = datetime.now(ET).strftime('%H:%M:%S')
    print(f'[{t}] {msg}', flush=True)


def tg_send(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import urllib.parse
        url  = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        data = json.dumps({'chat_id': TELEGRAM_CHAT_ID, 'text': msg}).encode()
        req  = urllib.request.Request(
            url, data=data, headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f'WARN tg_send: {e}')


def _to_f(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def build_event_ticker(game_date, away, home):
    # type: (date, str, str) -> str
    a   = MLB_TO_KALSHI.get(away, away)
    h   = MLB_TO_KALSHI.get(home, home)
    yy  = str(game_date.year)[2:]
    mon = MONTH_CODES[game_date.month]
    dd  = f'{game_date.day:02d}'
    return f'KXMLBMENTION-{yy}{mon}{dd}{a}{h}'


def get_market_price(k, ticker):
    # type: (KalshiClient, str) -> Optional[Dict]
    """Fetch a single Kalshi market's bid/ask/mid. Returns None on error."""
    try:
        mkt = k.get_market(ticker)
        if not mkt:
            return None
        bid = _to_f(mkt.get('yes_bid_dollars'))
        ask = _to_f(mkt.get('yes_ask_dollars'))
        mid = (bid + ask) / 2.0 if bid is not None and ask is not None and ask > 0 else None
        return {'bid': bid, 'ask': ask, 'mid': mid}
    except Exception as e:
        log(f'  WARN price fetch {ticker}: {e}')
        return None


# ── File I/O ──────────────────────────────────────────────────────────────────

PRICE_COLS = ['ts', 'game_pk', 'away', 'home', 'word', 'ticker', 'bid', 'ask', 'mid']

def ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def write_event(ev: dict) -> None:
    with open(EVENTS_FILE, 'a') as f:
        f.write(json.dumps(ev) + '\n')


def write_prices(rows: list) -> None:
    """Append a batch of price rows to the CSV. Writes header if file is new."""
    need_header = not os.path.exists(PRICES_FILE)
    with open(PRICES_FILE, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=PRICE_COLS)
        if need_header:
            w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, '') for c in PRICE_COLS})


# ── Game monitor ──────────────────────────────────────────────────────────────

class GameMonitor:
    """Tracks live state for one game."""

    def __init__(self, game_pk, away, home, kalshi_event_ticker, word_tickers, game_start_ts=0.0):
        # type: (int, str, str, str, Dict[str, str], float) -> None
        self.game_pk              = game_pk
        self.away                 = away
        self.home                 = home
        self.kalshi_event_ticker  = kalshi_event_ticker
        self.word_tickers         = word_tickers   # {word: market_ticker}
        self.game_start_ts        = game_start_ts  # Unix ts of scheduled first pitch
        self.price_cache          = {}             # {ticker: {bid,ask,mid}} — last seen price
        self.seen_keys            = set()          # type: Set[Tuple]
        self.extra_inning_fired   = False
        self.game_over            = False
        self.event_count          = 0
        self.price_row_count      = 0
        self.last_heartbeat_ts    = ts_now()

    def catchup(self, feed: dict) -> None:
        """Mark all already-completed plays as seen so we don't re-fire them."""
        plays     = feed.get('liveData', {}).get('plays', {}).get('allPlays', [])
        linescore = feed.get('liveData', {}).get('linescore', {})
        for play in plays:
            about = play.get('about', {})
            if not about.get('isComplete', False):
                continue
            ab = about.get('atBatIndex', -1)
            self.seen_keys.add((ab, 'result'))
            for pe in play.get('playEvents', []):
                if pe.get('type') == 'action':
                    self.seen_keys.add((ab, f'pe{pe.get("index", -1)}'))
        if (linescore.get('currentInning') or 0) > 9:
            self.extra_inning_fired = True
        log(f'  [{self.away}@{self.home}] catch-up: {len(self.seen_keys)} existing events skipped')

    def detect_events(self, feed: dict) -> List[Dict]:
        """Record every completed play. word/ticker populated when a Kalshi mapping exists."""
        events    = []
        plays     = feed.get('liveData', {}).get('plays', {}).get('allPlays', [])
        linescore = feed.get('liveData', {}).get('linescore', {})
        status    = feed.get('gameData', {}).get('status', {}).get('abstractGameState', '')

        if status == 'Final':
            self.game_over = True

        inning = linescore.get('currentInning') or 0
        if inning > 9 and not self.extra_inning_fired:
            self.extra_inning_fired = True
            events.append(self._make_event(
                'Extra Innings', f'Game enters inning {inning}',
                inning=inning, ab_idx=-1, linescore=linescore,
                word='extra inning', ticker=self.word_tickers.get('extra inning')))

        for play in plays:
            about  = play.get('about', {})
            result = play.get('result', {})
            ab_idx = about.get('atBatIndex', -1)

            if not about.get('isComplete', False):
                continue

            # ── At-bat result (every completed at-bat) ────────────────────────
            res_key = (ab_idx, 'result')
            if res_key not in self.seen_keys:
                self.seen_keys.add(res_key)
                ev_type    = result.get('event', '')
                desc       = result.get('description', '')
                inning_num = about.get('inning', 0)

                if ev_type == 'Home Run' and int(result.get('rbi') or 0) == 4:
                    word = 'grand slam'
                elif ev_type == 'Offensive Substitution':
                    word = 'pinch hit' if re.search(r'pinch.?hit', desc, re.IGNORECASE) else None
                else:
                    word = RESULT_WORD_MAP.get(ev_type)  # None for unmapped events

                events.append(self._make_event(
                    ev_type, desc, inning=inning_num, ab_idx=ab_idx,
                    linescore=linescore,
                    word=word, ticker=self.word_tickers.get(word) if word else None))

            # ── Mid-at-bat action events (every non-metadata action) ───────────
            for pe in play.get('playEvents', []):
                if pe.get('type') != 'action':
                    continue
                pe_idx = pe.get('index', -1)
                pe_key = (ab_idx, f'pe{pe_idx}')
                if pe_key in self.seen_keys:
                    continue
                self.seen_keys.add(pe_key)

                details    = pe.get('details', {})
                event_type = details.get('eventType', '')
                if event_type in ACTION_SKIP:
                    continue

                word = ACTION_WORD_MAP.get(event_type)
                events.append(self._make_event(
                    details.get('event', event_type),
                    details.get('description', ''),
                    inning=about.get('inning', 0),
                    ab_idx=ab_idx, pe_idx=pe_idx,
                    linescore=linescore,
                    word=word, ticker=self.word_tickers.get(word) if word else None))

        return events

    def _make_event(self, event_type, description, inning=0, ab_idx=-1,
                    pe_idx=-1, linescore=None, word=None, ticker=None):
        # type: (str, str, int, int, int, Optional[Dict], Optional[str], Optional[str]) -> Dict
        ts      = ts_now()
        ev_id   = f'{self.game_pk}_{ab_idx}_{pe_idx}_{int(ts * 1000)}'
        cached  = self.price_cache.get(ticker, {}) if ticker else {}
        ls      = linescore or {}
        offense = ls.get('offense', {})
        teams   = ls.get('teams', {})
        return {
            'event_id':            ev_id,
            'detected_ts':         ts,
            'game_pk':             self.game_pk,
            'away':                self.away,
            'home':                self.home,
            'kalshi_event_ticker': self.kalshi_event_ticker,
            'inning':              inning,
            'is_top_inning':       ls.get('isTopInning'),
            'outs':                ls.get('outs'),
            'away_score':          teams.get('away', {}).get('runs'),
            'home_score':          teams.get('home', {}).get('runs'),
            'on_1b':               bool(offense.get('onFirst')),
            'on_2b':               bool(offense.get('onSecond')),
            'on_3b':               bool(offense.get('onThird')),
            'ab_idx':              ab_idx,
            'pe_idx':              pe_idx,
            'event_type':          event_type,
            'description':         description[:200],
            'word':                word,    # Kalshi market label if known, else None
            'ticker':              ticker,
            'pre_bid':             cached.get('bid'),
            'pre_ask':             cached.get('ask'),
            'pre_mid':             cached.get('mid'),
        }

    def fetch_all_prices(self, k):
        # type: (KalshiClient) -> List[Dict]
        """Fetch prices for all markets in one bulk call. Returns rows for write_prices()."""
        ts = ts_now()
        try:
            mkts = k.get_markets_for_event(self.kalshi_event_ticker) or []
        except Exception as e:
            log(f'  WARN fetch_all_prices {self.kalshi_event_ticker}: {e}')
            mkts = []

        price_by_ticker = {}  # type: Dict[str, Dict]
        for mkt in mkts:
            ticker = mkt.get('ticker', '')
            bid    = _to_f(mkt.get('yes_bid_dollars'))
            ask    = _to_f(mkt.get('yes_ask_dollars'))
            mid    = (bid + ask) / 2.0 if bid is not None and ask is not None and ask > 0 else None
            if ticker:
                price_by_ticker[ticker] = {'bid': bid, 'ask': ask, 'mid': mid}

        rows = []
        for word, ticker in self.word_tickers.items():
            price = price_by_ticker.get(ticker)
            if price:
                self.price_cache[ticker] = price
            rows.append({
                'ts':      ts,
                'game_pk': self.game_pk,
                'away':    self.away,
                'home':    self.home,
                'word':    word,
                'ticker':  ticker,
                'bid':     price.get('bid', '') if price else '',
                'ask':     price.get('ask', '') if price else '',
                'mid':     price.get('mid', '') if price else '',
            })
        return rows


# ── Market discovery ──────────────────────────────────────────────────────────

def find_game_markets(k, event_ticker):
    # type: (KalshiClient, str) -> Dict[str, str]
    try:
        markets = k.get_markets_for_event(event_ticker)
    except Exception as e:
        log(f'  WARN get_markets_for_event({event_ticker}): {e}')
        return {}
    result = {}
    for mkt in markets:
        ticker = mkt.get('ticker', '')
        suffix = ticker.rsplit('-', 1)[-1] if '-' in ticker else ''
        word   = TICKER_SUFFIX_TO_WORD.get(suffix)
        if word:
            result[word] = ticker
    return result


def _parse_event_ticker(ticker):
    # type: (str) -> Optional[tuple]
    """Parse KXMLBMENTION-26JUN18TEXBOS → (date, away_mlb, home_mlb), or None."""
    if not ticker.startswith('KXMLBMENTION-'):
        return None
    body = ticker[len('KXMLBMENTION-'):]  # '26JUN18TEXBOS'
    if len(body) < 11:
        return None
    yy, mon, dd = body[:2], body[2:5], body[5:7]
    if mon not in MONTH_TO_NUM:
        return None
    try:
        game_date = date(2000 + int(yy), MONTH_TO_NUM[mon], int(dd))
    except ValueError:
        return None
    teams_str = body[7:]
    for split in (2, 3):
        away_k = teams_str[:split]
        home_k = teams_str[split:]
        away_ok = (len(away_k) == 2 and away_k in KALSHI_2CHAR) or len(away_k) == 3
        home_ok = (len(home_k) == 2 and home_k in KALSHI_2CHAR) or len(home_k) == 3
        if away_ok and home_ok and len(home_k) in (2, 3):
            return (game_date,
                    KALSHI_TO_MLB.get(away_k, away_k),
                    KALSHI_TO_MLB.get(home_k, home_k))
    return None


def find_games_from_kalshi(k, game_date):
    # type: (KalshiClient, date) -> List[Dict]
    """Find national games by querying open KXMLBMENTION events from Kalshi.
    Replaces find_national_games — avoids hydrate=broadcasts which MLB API blocks."""
    try:
        events = k.get_events('KXMLBMENTION', status='open')
    except Exception as e:
        log(f'  WARN: Kalshi get_events failed: {e}')
        return []

    today_events = []
    for ev in events:
        ticker = ev.get('event_ticker', '')
        parsed = _parse_event_ticker(ticker)
        if parsed is None:
            continue
        ev_date, away_mlb, home_mlb = parsed
        if ev_date == game_date:
            today_events.append((away_mlb, home_mlb))

    if not today_events:
        return []

    # Look up game_pk from MLB (hydrate=team is sufficient; no broadcasts needed)
    url      = (f'https://statsapi.mlb.com/api/v1/schedule?sportId=1'
                f'&date={game_date}&hydrate=team')
    mlb_data = get_json(url)
    mlb_index = {}  # type: Dict[tuple, tuple]
    if mlb_data:
        for day in mlb_data.get('dates', []):
            for g in day.get('games', []):
                teams  = g.get('teams', {})
                away   = teams.get('away', {}).get('team', {}).get('abbreviation', '')
                home   = teams.get('home', {}).get('team', {}).get('abbreviation', '')
                pk     = g.get('gamePk')
                status = g.get('status', {}).get('abstractGameState', 'Preview')
                try:
                    s = g.get('gameDate', '').replace('Z', '+00:00')
                    game_start_ts = datetime.fromisoformat(s).timestamp()
                except Exception:
                    game_start_ts = 0.0
                mlb_index[(away, home)] = (pk, status, game_start_ts)

    games = []
    for away_mlb, home_mlb in today_events:
        pk, status, game_start_ts = mlb_index.get((away_mlb, home_mlb), (None, 'Preview', 0.0))
        if pk is None:
            log(f'  WARN: no MLB game_pk for {away_mlb}@{home_mlb} on {game_date}')
            continue
        games.append({
            'game_pk':       pk,
            'away':          away_mlb,
            'home':          home_mlb,
            'status':        status,
            'networks':      [],
            'game_start_ts': game_start_ts,
        })
    return games


def find_national_games(game_date):
    # type: (date) -> List[Dict]
    # Kept for reference; not used — MLB hydrate=broadcasts returns 406.
    url  = (f'https://statsapi.mlb.com/api/v1/schedule?sportId=1'
            f'&date={game_date}&hydrate=team,broadcasts,linescore')
    data = get_json(url)
    if not data:
        return []
    games = []
    for day in data.get('dates', []):
        for g in day.get('games', []):
            broadcasts = g.get('broadcasts') or []
            tv_en      = [b for b in broadcasts
                          if b.get('type') == 'TV' and b.get('language', 'en') == 'en']
            national   = [b for b in tv_en if b.get('isNational')]
            if not national:
                continue
            teams  = g.get('teams', {})
            away   = teams.get('away', {}).get('team', {}).get('abbreviation', '')
            home   = teams.get('home', {}).get('team', {}).get('abbreviation', '')
            status = g.get('status', {}).get('abstractGameState', 'Preview')
            games.append({
                'game_pk':  g.get('gamePk'),
                'away':     away,
                'home':     home,
                'status':   status,
                'networks': [b['name'] for b in national],
            })
    return games


# ── Main loop helpers ─────────────────────────────────────────────────────────

DAILY_START_HOUR  = 10    # 10am ET — time to begin each day's game search
MARKET_RETRY      = 1800  # 30 min between attempts to find Kalshi markets for pending games
PENDING_TIMEOUT   = 4 * 3600  # drop pending game if no markets found 4h past scheduled start


def _activate_game(k, g, today):
    # type: (KalshiClient, Dict, date) -> Optional[GameMonitor]
    """Try to create a GameMonitor for game g. Returns None if no Kalshi markets yet."""
    ev_ticker    = build_event_ticker(today, g['away'], g['home'])
    word_tickers = find_game_markets(k, ev_ticker)
    if not word_tickers:
        return None
    log(f'  Activated {ev_ticker}: {len(word_tickers)} markets')
    gm   = GameMonitor(g['game_pk'], g['away'], g['home'], ev_ticker, word_tickers,
                       g.get('game_start_ts', 0.0))
    feed = get_json(f'https://statsapi.mlb.com/api/v1.1/game/{gm.game_pk}/feed/live')
    if feed:
        gm.catchup(feed)
    if ts_now() >= gm.game_start_ts:
        rows = gm.fetch_all_prices(k)
        write_prices(rows)
        log(f'  [{gm.away}@{gm.home}] seeded '
            f'{sum(1 for r in rows if r.get("mid") != "")}/{len(rows)} prices')
    else:
        start_et = datetime.fromtimestamp(gm.game_start_ts, ET).strftime('%H:%M ET')
        log(f'  [{gm.away}@{gm.home}] first pitch {start_et} — price collection starts then')
    return gm


def _poll_game(k, gm):
    # type: (KalshiClient, GameMonitor) -> List[Dict]
    """One poll cycle for a game: fetch feed + prices. Returns new events."""
    if ts_now() < gm.game_start_ts:
        return []

    feed = get_json(f'https://statsapi.mlb.com/api/v1.1/game/{gm.game_pk}/feed/live')
    new_events = []
    if feed:
        new_events = gm.detect_events(feed)
        for ev in new_events:
            half  = 'top' if ev.get('is_top_inning') else 'bot'
            score = f'{ev.get("away_score")}-{ev.get("home_score")}'
            bases = ', '.join(
                b for b, v in [('1st', ev.get('on_1b')),
                               ('2nd', ev.get('on_2b')),
                               ('3rd', ev.get('on_3b'))] if v
            ) or 'empty'
            log(f'  EVENT [{gm.away}@{gm.home}] inn={ev["inning"]}({half}) '
                f'score={score} outs={ev.get("outs")} bases={bases}  '
                f'{ev["event_type"]} -> {ev.get("word") or ""}  '
                f'pre_mid={ev["pre_mid"]}')
            write_event(ev)
            gm.event_count += 1
    rows = gm.fetch_all_prices(k)
    write_prices(rows)
    gm.price_row_count += len(rows)
    log(f'  [{gm.away}@{gm.home}] '
        + '  '.join(f'{r["word"][:4]}={r["mid"]}' for r in rows if r.get('mid') != ''))
    return new_events


def _sleep_until_tomorrow(today):
    # type: (date) -> None
    tomorrow  = today + timedelta(days=1)
    wake_time = datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                         DAILY_START_HOUR, 0, 0, tzinfo=ET)
    secs = max(0.0, (wake_time - datetime.now(ET)).total_seconds())
    log(f'Sleeping {secs/3600:.1f}h until {wake_time.strftime("%Y-%m-%d %H:%M ET")}')
    tg_send(f'All done for {today}. Next check: {wake_time.strftime("%b %d at %I:%M%p ET")}')
    time.sleep(secs)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ensure_data_dir()

    if not KALSHI_KEY_ID or not KALSHI_KEY_PATH:
        print('ERROR: KALSHI_KEY_ID and KALSHI_KEY_PATH must be set')
        sys.exit(1)

    k = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH)
    log(f'Latency collection started (weekly mode) — {datetime.now(ET).date()}')
    log(f'Events -> {EVENTS_FILE}')
    log(f'Prices -> {PRICES_FILE}')

    try:
        while True:
            today      = datetime.now(ET).date()
            games_info = find_games_from_kalshi(k, today)

            if not games_info:
                log(f'No national games on {today}')
                tg_send(f'No nationally televised games on {today}. Next check tomorrow.')
                _sleep_until_tomorrow(today)
                continue

            log(f'Found {len(games_info)} national game(s) on {today}:')
            for g in games_info:
                log(f'  {g["away"]}@{g["home"]}  status={g["status"]}')

            lines = [f"Todays Games — {today.strftime('%B %-d')}"]
            for g in games_info:
                lines.append(f'  {g["away"]}@{g["home"]}')
            tg_send('\n'.join(lines))

            # pending: national games not yet on Kalshi (or not yet started)
            pending       = [g for g in games_info if g['status'] != 'Final']
            monitors      = []   # type: List[GameMonitor]
            last_retry_ts = 0.0  # force immediate first attempt

            while pending or monitors:

                # ── Expire pending games that are well past their start time ──
                now_ts = ts_now()
                expired = [g for g in pending
                           if g.get('game_start_ts', 0) > 0
                           and now_ts > g['game_start_ts'] + PENDING_TIMEOUT]
                if expired:
                    pending = [g for g in pending if g not in expired]
                    for g in expired:
                        name = f'{g["away"]}@{g["home"]}'
                        log(f'  {name} — no Kalshi markets found {PENDING_TIMEOUT // 3600}h past first pitch, giving up')
                        tg_send(f'Gave up waiting for Kalshi markets: {name}')

                # ── Try activating pending games (every MARKET_RETRY seconds) ──
                if pending and ts_now() - last_retry_ts >= MARKET_RETRY:
                    last_retry_ts  = ts_now()
                    still_pending  = []
                    for g in pending:
                        gm = _activate_game(k, g, today)
                        if gm:
                            monitors.append(gm)
                        else:
                            log(f'  {g["away"]}@{g["home"]} — no Kalshi markets yet')
                            still_pending.append(g)
                    pending = still_pending

                # ── If nothing active yet, wait and retry ─────────────────────
                if not monitors:
                    wait = max(60.0, MARKET_RETRY - (ts_now() - last_retry_ts))
                    log(f'No active markets yet — retrying in {wait/60:.0f} min')
                    time.sleep(wait)
                    continue

                # ── Poll all active monitors ───────────────────────────────────
                loop_start = ts_now()
                still_live = []

                for gm in monitors:
                    _poll_game(k, gm)

                    if ts_now() >= gm.game_start_ts and ts_now() - gm.last_heartbeat_ts >= HEARTBEAT_INTERVAL:
                        gm.last_heartbeat_ts = ts_now()
                        snaps = gm.price_row_count // max(len(gm.word_tickers), 1)
                        tg_send(
                            f'Heartbeat: {gm.away}@{gm.home}\n'
                            f'Events: {gm.event_count}  Snapshots: {snaps}\n'
                            f'Still monitoring...'
                        )

                    if not gm.game_over:
                        still_live.append(gm)
                    else:
                        log(f'  [{gm.away}@{gm.home}] Final')
                        snaps = gm.price_row_count // max(len(gm.word_tickers), 1)
                        tg_send(
                            f'FINAL: {gm.away}@{gm.home}\n'
                            f'Events: {gm.event_count}  Snapshots: {snaps}'
                        )

                monitors = still_live
                time.sleep(max(0.0, POLL_INTERVAL - (ts_now() - loop_start)))

            # All games done for today
            _sleep_until_tomorrow(today)

    except KeyboardInterrupt:
        log('Interrupted.')
        tg_send('Latency collection stopped.')


if __name__ == '__main__':
    main()
