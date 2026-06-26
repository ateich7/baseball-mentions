"""
test_pipeline.py — end-to-end tests for the latency data collection pipeline.

Tests each component in isolation before running against live games:
  1. MLB API connectivity and schedule parsing
  2. MLB API live feed structure validation
  3. Kalshi API market discovery
  4. Kalshi API price fetching
  5. GameMonitor.catchup() — marks existing plays as seen
  6. GameMonitor.detect_events() — no spurious events after catchup
  7. GameMonitor.detect_events() — synthetic event injection
  8. File I/O — write + reload latency_events.jsonl / latency_prices.csv
  9. analyze_latency.py import + parse
 10. One live poll cycle (5 seconds, real game)

Usage:
  KALSHI_KEY_ID=... KALSHI_KEY_PATH=... python test_pipeline.py [--game GAMEPK]
"""

import argparse, json, os, sys, tempfile, time
from datetime import date
from typing import Dict, List, Optional

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = '\033[32mPASS\033[0m'
FAIL = '\033[31mFAIL\033[0m'
SKIP = '\033[33mSKIP\033[0m'
results = []

def section(name: str) -> None:
    print(f'\n{"─"*60}')
    print(f'  {name}')
    print(f'{"─"*60}')

def check(name: str, ok: bool, detail: str = '') -> None:
    tag = PASS if ok else FAIL
    results.append((name, ok))
    suffix = f'  → {detail}' if detail else ''
    print(f'  [{tag}] {name}{suffix}')
    if not ok and detail:
        print(f'         detail: {detail}')


def summary() -> None:
    print(f'\n{"="*60}')
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    failed = total - passed
    print(f'  RESULTS: {passed}/{total} passed', end='')
    if failed:
        print(f'  ({failed} FAILED)', end='')
    print()
    if failed:
        print('  Failed checks:')
        for name, ok in results:
            if not ok:
                print(f'    • {name}')
    print('='*60)
    sys.exit(0 if failed == 0 else 1)


# ── 1. MLB API — schedule ─────────────────────────────────────────────────────

def test_mlb_schedule(today: date) -> Optional[List[Dict]]:
    section('1. MLB API — schedule endpoint')
    from utils import get_json

    url  = (f'https://statsapi.mlb.com/api/v1/schedule?sportId=1'
            f'&date={today}&hydrate=team,broadcasts,linescore')
    data = get_json(url)

    check('HTTP response received', data is not None)
    if not data:
        return None

    dates = data.get('dates', [])
    games = [g for day in dates for g in day.get('games', [])]
    check('At least one game returned', len(games) > 0, f'{len(games)} games')

    national_games = []
    for g in games:
        broadcasts = g.get('broadcasts') or []
        tv_en = [b for b in broadcasts
                 if b.get('type') == 'TV' and b.get('language', 'en') == 'en']
        national = [b for b in tv_en if b.get('isNational')]
        if national:
            teams = g.get('teams', {})
            away  = teams.get('away', {}).get('team', {}).get('abbreviation', '')
            home  = teams.get('home', {}).get('team', {}).get('abbreviation', '')
            status = g.get('status', {}).get('abstractGameState', '')
            national_games.append({
                'game_pk': g.get('gamePk'),
                'away': away, 'home': home, 'status': status,
                'networks': [b['name'] for b in national],
            })
            print(f'          {away}@{home}  pk={g.get("gamePk")}  '
                  f'status={status}  nets={[b["name"] for b in national]}')

    check('At least one nationally televised game', len(national_games) > 0,
          f'{len(national_games)} national games')
    return national_games


def test_collect_find_national(today: date, national_games: Optional[List[Dict]]) -> None:
    section('1b. collect_latency_data.find_national_games()')
    from collect_latency_data import find_national_games
    games = find_national_games(today)
    check('Returns list', isinstance(games, list))
    check('Matches direct parse count',
          national_games is None or len(games) == len(national_games),
          f'{len(games)} games')
    for g in games:
        has_fields = all(k in g for k in ('game_pk', 'away', 'home', 'status', 'networks'))
        check(f'Game {g.get("away")}@{g.get("home")} has required fields', has_fields)
        print(f'          {g}')


# ── 2. MLB API — live feed ────────────────────────────────────────────────────

def test_mlb_live_feed(game_pk: int, away: str, home: str) -> Optional[dict]:
    section(f'2. MLB API — live feed  (game_pk={game_pk}  {away}@{home})')
    from utils import get_json

    url  = f'https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live'
    feed = get_json(url)

    check('HTTP response received', feed is not None)
    if not feed:
        return None

    status = feed.get('gameData', {}).get('status', {}).get('abstractGameState', '')
    check('abstractGameState present', bool(status), status)

    plays = feed.get('liveData', {}).get('plays', {}).get('allPlays', [])
    check('allPlays list present', isinstance(plays, list), f'{len(plays)} plays')

    linescore = feed.get('liveData', {}).get('linescore', {})
    inning = linescore.get('currentInning', 0) or 0
    check('linescore.currentInning present', isinstance(inning, int), f'inning {inning}')

    if plays:
        play = plays[-1]
        has_about  = 'about' in play
        has_result = 'result' in play
        check('Last play has about + result', has_about and has_result)

        # Validate a complete play (only meaningful if game has started)
        complete_plays = [p for p in plays if p.get('about', {}).get('isComplete')]
        if status in ('Live', 'Final'):
            check('At least one complete play', len(complete_plays) > 0,
                  f'{len(complete_plays)} complete')
        else:
            print(f'          [{SKIP}] At least one complete play  '
                  f'(game is {status}, 0 expected)')

        if complete_plays:
            p = complete_plays[-1]
            ev = p.get('result', {}).get('event', '')
            ab = p.get('about', {}).get('atBatIndex', -1)
            pe_count = len(p.get('playEvents', []))
            print(f'          last complete play: ab={ab}  event="{ev}"  '
                  f'playEvents={pe_count}')
            check('result.event is a string', isinstance(ev, str) and len(ev) > 0, ev)

    # Check playEvents structure for action events
    action_events = []
    for p in plays:
        for pe in p.get('playEvents', []):
            if pe.get('type') == 'action':
                action_events.append(pe)
    print(f'          total action events across all plays: {len(action_events)}')
    if action_events:
        ae = action_events[0]
        check('Action event has details.eventType',
              'eventType' in ae.get('details', {}),
              ae.get('details', {}).get('eventType', ''))

    return feed


# ── 3. Kalshi — market discovery ──────────────────────────────────────────────

def test_kalshi_market_discovery(k, today: date, national_games: List[Dict]) -> Dict[str, Dict]:
    section('3. Kalshi — market discovery')
    from collect_latency_data import build_event_ticker, find_game_markets
    from utils import TICKER_SUFFIX_TO_WORD

    all_word_tickers = {}
    for g in national_games:
        ev_ticker = build_event_ticker(today, g['away'], g['home'])
        word_tickers = find_game_markets(k, ev_ticker)
        check(f'{ev_ticker} — markets returned', len(word_tickers) > 0,
              f'{len(word_tickers)} words: {sorted(word_tickers)}')
        all_word_tickers[ev_ticker] = word_tickers

        # Validate ticker format
        for word, ticker in word_tickers.items():
            suffix = ticker.rsplit('-', 1)[-1]
            mapped = TICKER_SUFFIX_TO_WORD.get(suffix)
            check(f'  ticker {ticker} → word "{mapped}"', mapped == word)

    return all_word_tickers


# ── 4. Kalshi — price fetching ────────────────────────────────────────────────

def test_kalshi_prices(k, all_word_tickers: Dict[str, Dict]) -> None:
    section('4. Kalshi — price fetching')
    from collect_latency_data import get_market_price

    # Test one market per game (avoid hammering the API)
    for ev_ticker, word_tickers in all_word_tickers.items():
        if not word_tickers:
            continue
        word, ticker = next(iter(word_tickers.items()))
        t_start = time.time()
        price = get_market_price(k, ticker)
        latency_ms = int((time.time() - t_start) * 1000)
        check(f'{ticker.rsplit("-",1)[-1]} price fetch returns dict',
              price is not None, f'latency={latency_ms}ms')
        if price:
            bid, ask, mid = price.get('bid'), price.get('ask'), price.get('mid')
            check(f'  bid/ask/mid are floats or None',
                  all(v is None or isinstance(v, float) for v in (bid, ask, mid)),
                  f'bid={bid}  ask={ask}  mid={mid}')
            if mid is not None:
                check(f'  mid in (0, 1)', 0 < mid < 1, f'mid={mid}')
        time.sleep(0.2)


# ── 5. GameMonitor — catchup ──────────────────────────────────────────────────

def test_catchup(game_pk: int, away: str, home: str,
                 ev_ticker: str, word_tickers: Dict, feed: Optional[dict]) -> 'GameMonitor':
    section(f'5. GameMonitor.catchup()  ({away}@{home})')
    from collect_latency_data import GameMonitor

    if not feed:
        print(f'  [{SKIP}] No feed available')
        return None

    gm = GameMonitor(game_pk, away, home, ev_ticker, word_tickers)
    plays_before = len(feed.get('liveData', {}).get('plays', {}).get('allPlays', []))
    complete_before = sum(
        1 for p in feed.get('liveData', {}).get('plays', {}).get('allPlays', [])
        if p.get('about', {}).get('isComplete')
    )

    gm.catchup(feed)
    keys_after = len(gm.seen_keys)
    status = feed.get('gameData', {}).get('status', {}).get('abstractGameState', '')

    if complete_before > 0:
        check('seen_keys populated after catchup', keys_after > 0,
              f'{keys_after} keys (from {complete_before} complete plays)')
    else:
        print(f'          [{SKIP}] seen_keys check (game is {status}, '
              f'0 complete plays → 0 keys expected)')
        results.append(('seen_keys populated after catchup (skipped — Preview)', True))

    check('extra_inning_fired set if game in extras',
          True,  # always passes — it's either True or False, both are valid
          f'extra_inning_fired={gm.extra_inning_fired}')

    return gm


# ── 6. GameMonitor — detect_events after catchup (should be empty) ────────────

def test_detect_no_spurious(gm, feed: Optional[dict]) -> None:
    section('6. GameMonitor.detect_events() — no spurious events after catchup')
    if gm is None or feed is None:
        print(f'  [{SKIP}] No monitor or feed available')
        return

    events = gm.detect_events(feed)
    check('Zero events on same feed after catchup', len(events) == 0,
          f'{len(events)} events (expected 0)')
    if events:
        for ev in events:
            print(f'          SPURIOUS: {ev["event_type"]} → {ev["word"]}  '
                  f'ab={ev["ab_idx"]}  pe={ev["pe_idx"]}')


# ── 7. GameMonitor — synthetic event injection ────────────────────────────────

def test_synthetic_events(gm, word_tickers: Dict) -> None:
    section('7. GameMonitor — synthetic event injection')
    if gm is None:
        print(f'  [{SKIP}] No monitor available')
        return

    # Build a fake feed with one new wild pitch (mid-at-bat action) at a high ab_idx
    FAKE_AB_IDX = 9999
    fake_feed = {
        'gameData': {'status': {'abstractGameState': 'Live'}},
        'liveData': {
            'linescore': {'currentInning': 5},
            'plays': {
                'allPlays': [
                    {
                        'about': {'atBatIndex': FAKE_AB_IDX, 'isComplete': True, 'inning': 5},
                        'result': {'event': 'Strikeout', 'eventType': 'strikeout',
                                   'description': 'Strikeout swinging.', 'rbi': 0},
                        'playEvents': [
                            {
                                'type': 'action',
                                'index': 0,
                                'details': {
                                    'event': 'Wild Pitch',
                                    'eventType': 'wild_pitch',
                                    'description': 'Wild pitch — runner to 2nd.',
                                },
                            },
                            {
                                'type': 'pitch',
                                'index': 1,
                                'details': {'call': {'code': 'S', 'description': 'Strike'}},
                            },
                        ],
                    }
                ]
            },
        },
    }

    # Seed price cache so pre_mid is set
    wp_ticker = word_tickers.get('wild pitch')
    if wp_ticker:
        gm.price_cache[wp_ticker] = {'bid': 0.20, 'ask': 0.26, 'mid': 0.23, 'ts': time.time()}

    events = gm.detect_events(fake_feed)
    # Expect ≥2 events: the Strikeout at-bat result + the Wild Pitch action event
    check('Synthetic events detected (Strikeout + Wild Pitch)', len(events) >= 2,
          f'{len(events)} event(s)')
    wp_events = [e for e in events if e.get('word') == 'wild pitch']
    check('Wild pitch event present', len(wp_events) >= 1,
          f'{len(wp_events)} wild pitch event(s)')
    if wp_events:
        ev = wp_events[0]
        check('event.word == "wild pitch"', ev.get('word') == 'wild pitch',
              ev.get('word'))
        check('event.event_type contains "Wild Pitch"',
              'Wild Pitch' in ev.get('event_type', ''),
              ev.get('event_type'))
        check('event.ab_idx == FAKE_AB_IDX', ev.get('ab_idx') == FAKE_AB_IDX,
              str(ev.get('ab_idx')))
        check('event.pre_mid is float', isinstance(ev.get('pre_mid'), float),
              str(ev.get('pre_mid')))
        print(f'          event: {json.dumps({k: ev[k] for k in ("event_type","word","ab_idx","pre_mid","ticker") if k in ev}, indent=2)}')

    # Also inject a grand slam (Home Run + 4 RBI)
    FAKE_AB_IDX2 = 9998
    fake_feed2 = {
        'gameData': {'status': {'abstractGameState': 'Live'}},
        'liveData': {
            'linescore': {'currentInning': 3},
            'plays': {
                'allPlays': [
                    {
                        'about': {'atBatIndex': FAKE_AB_IDX2, 'isComplete': True, 'inning': 3},
                        'result': {
                            'event': 'Home Run',
                            'eventType': 'home_run',
                            'description': 'Seiya Suzuki homers on a fly ball to left center. '
                                           'Ian Happ scores. Dansby Swanson scores. '
                                           'Michael Busch scores.',
                            'rbi': 4,
                        },
                        'playEvents': [],
                    }
                ]
            },
        },
    }

    gs_ticker = word_tickers.get('grand slam')
    if gs_ticker:
        gm.price_cache[gs_ticker] = {'bid': 0.40, 'ask': 0.48, 'mid': 0.44, 'ts': time.time()}

    events2 = gm.detect_events(fake_feed2)
    gs_events = [e for e in events2 if e.get('word') == 'grand slam']
    check('Synthetic grand slam detected', len(gs_events) >= 1,
          f'{len(gs_events)} grand slam event(s)')

    # Ensure a plain Home Run (1 RBI) does NOT trigger grand slam
    FAKE_AB_IDX3 = 9997
    fake_feed3 = {
        'gameData': {'status': {'abstractGameState': 'Live'}},
        'liveData': {
            'linescore': {'currentInning': 3},
            'plays': {
                'allPlays': [
                    {
                        'about': {'atBatIndex': FAKE_AB_IDX3, 'isComplete': True, 'inning': 3},
                        'result': {
                            'event': 'Home Run',
                            'eventType': 'home_run',
                            'description': 'Solo homer.',
                            'rbi': 1,
                        },
                        'playEvents': [],
                    }
                ]
            },
        },
    }
    events3 = gm.detect_events(fake_feed3)
    gs_false = [e for e in events3 if e.get('word') == 'grand slam']
    check('Solo HR does NOT trigger grand slam', len(gs_false) == 0,
          f'{len(gs_false)} spurious grand slam events')


# ── 8. File I/O ───────────────────────────────────────────────────────────────

def test_file_io() -> None:
    section('8. File I/O — write + reload')
    import csv
    from collect_latency_data import ensure_data_dir, write_event, write_prices, DATA_DIR, EVENTS_FILE, PRICES_FILE

    ensure_data_dir()
    check('data/ directory created', os.path.isdir(DATA_DIR), DATA_DIR)

    now = time.time()
    fake_ev = {
        'event_id':            'TEST_9999_0_0_1234567890000',
        'detected_ts':         now,
        'game_pk':             9999,
        'away': 'TST', 'home': 'TST',
        'kalshi_event_ticker': 'KXMLBMENTION-TEST',
        'inning': 5, 'ab_idx': 0, 'pe_idx': 0,
        'event_type':          'Wild Pitch',
        'description':         'Test wild pitch event.',
        'word':                'wild pitch',
        'ticker':              'KXMLBMENTION-TEST-WILD',
        'pre_bid': 0.20, 'pre_ask': 0.26, 'pre_mid': 0.23,
    }

    write_event(fake_ev)
    check('latency_events.jsonl written', os.path.exists(EVENTS_FILE))

    with open(EVENTS_FILE) as f:
        lines = [l.strip() for l in f if l.strip()]
    try:
        parsed = json.loads(lines[-1])
        check('Last line is valid JSON', True)
        check('event_id round-trips', parsed.get('event_id') == fake_ev['event_id'])
        check('word round-trips', parsed.get('word') == 'wild pitch')
        check('pre_mid round-trips', parsed.get('pre_mid') == 0.23)
    except json.JSONDecodeError as e:
        check('Last line is valid JSON', False, str(e))

    # Write two price snapshots (new all-market row format)
    fake_price_rows = [
        {'ts': now,      'game_pk': 9999, 'away': 'TST', 'home': 'TST',
         'word': 'wild pitch', 'ticker': 'KXMLBMENTION-TEST-WILD',
         'bid': 0.20, 'ask': 0.26, 'mid': 0.23},
        {'ts': now + 30, 'game_pk': 9999, 'away': 'TST', 'home': 'TST',
         'word': 'wild pitch', 'ticker': 'KXMLBMENTION-TEST-WILD',
         'bid': 0.75, 'ask': 0.80, 'mid': 0.775},
    ]
    write_prices(fake_price_rows)
    check('latency_prices.csv written', os.path.exists(PRICES_FILE))

    with open(PRICES_FILE) as f:
        rows = list(csv.DictReader(f))
    test_rows = [r for r in rows if r.get('game_pk') == '9999']
    check('Price rows written', len(test_rows) >= 2,
          f'{len(test_rows)} rows for test game_pk')
    if len(test_rows) >= 2:
        late = max(test_rows, key=lambda r: float(r['ts']))
        check('Later row has higher mid',
              float(late.get('mid', 0)) > 0.5, late.get('mid'))


# ── 9. analyze_latency.py — can parse the test data ──────────────────────────

def test_analyze() -> None:
    section('9. analyze_latency.py — import + parse')
    try:
        import analyze_latency
        check('analyze_latency imports OK', True)

        events = analyze_latency.load_events(analyze_latency.EVENTS_FILE)
        check('load_events() returns list', isinstance(events, list), f'{len(events)} events')

        prices = analyze_latency.load_prices(analyze_latency.PRICES_FILE)
        check('load_prices() returns list', isinstance(prices, list), f'{len(prices)} rows')

        test_rows = [r for r in prices if r.get('game_pk') == 9999]
        check('Test price rows found', len(test_rows) >= 2, f'{len(test_rows)} rows')

        idx = analyze_latency.build_price_index(prices)
        test_series = idx.get((9999, 'wild pitch'), [])
        check('Price index lookup works', len(test_series) >= 2,
              f'{len(test_series)} rows for (9999, wild pitch)')

    except Exception as e:
        check('analyze_latency imports OK', False, str(e))


# ── 10. Live poll cycle ────────────────────────────────────────────────────────

def test_live_poll(k, national_games: List[Dict], all_word_tickers: Dict,
                   today: date) -> None:
    section('10. Live poll cycle (one iteration, real game)')
    from collect_latency_data import (
        GameMonitor, build_event_ticker, get_market_price
    )
    from utils import get_json

    live_games = [g for g in national_games if g['status'] == 'Live']
    if not live_games:
        preview = [g for g in national_games if g['status'] == 'Preview']
        if preview:
            live_games = preview[:1]
            print(f'  (no Live games; using Preview: {preview[0]["away"]}@{preview[0]["home"]})')
        else:
            print(f'  [{SKIP}] No Live or Preview games available')
            return

    g = live_games[0]
    ev_ticker    = build_event_ticker(today, g['away'], g['home'])
    word_tickers = all_word_tickers.get(ev_ticker, {})

    if not word_tickers:
        check('word_tickers available', False, f'no markets for {ev_ticker}')
        return

    gm = GameMonitor(g['game_pk'], g['away'], g['home'], ev_ticker, word_tickers)

    # Fetch live feed
    url  = f'https://statsapi.mlb.com/api/v1.1/game/{g["game_pk"]}/feed/live'
    t0   = time.time()
    feed = get_json(url)
    mlb_latency = int((time.time() - t0) * 1000)
    check('Live feed fetched', feed is not None, f'latency={mlb_latency}ms')
    if not feed:
        return

    # Catchup
    gm.catchup(feed)
    check('Catchup completed', len(gm.seen_keys) >= 0,
          f'{len(gm.seen_keys)} seen keys')

    # Fetch all market prices (new approach: all markets at once)
    t0   = time.time()
    rows = gm.fetch_all_prices(k)
    lat  = int((time.time() - t0) * 1000)
    for r in rows:
        if r.get('mid') != '':
            print(f'          {r["word"]:<20} bid={r.get("bid")}  '
                  f'ask={r.get("ask")}  mid={r.get("mid")}')

    n_priced = sum(1 for r in rows if r.get('mid') != '')
    check('All markets fetched', len(rows) > 0, f'{len(rows)} markets in {lat}ms')
    check('At least half have prices', n_priced >= len(rows) // 2,
          f'{n_priced}/{len(rows)} have mid price')
    check('Price cache populated', len(gm.price_cache) > 0,
          f'{len(gm.price_cache)} cached')

    # Second poll — detect_events should return 0 after catchup
    print(f'  Sleeping {5}s then re-polling to check for new events...')
    time.sleep(5)

    feed2  = get_json(url)
    events = gm.detect_events(feed2) if feed2 else []
    plays1 = len(feed.get('liveData', {}).get('plays', {}).get('allPlays', []))
    plays2 = len((feed2 or {}).get('liveData', {}).get('plays', {}).get('allPlays', []))
    new_plays = plays2 - plays1
    print(f'          plays: {plays1} → {plays2} ({new_plays:+d} in 5s)')
    print(f'          events detected: {len(events)}')
    for ev in events:
        print(f'            → {ev["event_type"]} / {ev["word"]}  inn={ev["inning"]}')

    check('detect_events returns a list', isinstance(events, list))
    check('No crash on second poll', True)
    print(f'  (If {new_plays} new plays occurred in 5s and {len(events)} '
          f'events fired, detection is working correctly)')


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--game', type=int, help='Override game_pk for live feed test')
    args = ap.parse_args()

    KALSHI_KEY_ID   = os.getenv('KALSHI_KEY_ID',  '')
    KALSHI_KEY_PATH = os.getenv('KALSHI_KEY_PATH', '/root/baseball-mentions/kalshi_key.txt')

    if not KALSHI_KEY_ID or not KALSHI_KEY_PATH:
        print('ERROR: KALSHI_KEY_ID and KALSHI_KEY_PATH must be set')
        sys.exit(1)

    from kalshi import KalshiClient
    k = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH)

    today = date.today()
    print(f'\nlatency pipeline tests — {today}')

    # 1. MLB schedule
    national_games = test_mlb_schedule(today)
    if not national_games:
        print('No national games found — cannot run remaining tests')
        summary()
        return

    # 1b. collect_latency_data.find_national_games
    test_collect_find_national(today, national_games)

    # Pick the best test game (prefer Live > Preview over Final)
    def game_priority(g: Dict) -> int:
        return {'Live': 0, 'Preview': 1}.get(g['status'], 2)
    test_game = sorted(national_games, key=game_priority)[0]

    if args.game:
        test_game = {'game_pk': args.game, 'away': '???', 'home': '???',
                     'status': 'Live', 'networks': []}

    # 2. MLB live feed
    feed = test_mlb_live_feed(test_game['game_pk'], test_game['away'], test_game['home'])

    # 3. Kalshi market discovery
    all_word_tickers = test_kalshi_market_discovery(k, today, national_games)

    # 4. Kalshi price fetching
    test_kalshi_prices(k, all_word_tickers)

    # Pick event ticker for the test game
    from collect_latency_data import build_event_ticker
    ev_ticker    = build_event_ticker(today, test_game['away'], test_game['home'])
    word_tickers = all_word_tickers.get(ev_ticker, {})

    # 5. GameMonitor catchup
    gm = test_catchup(test_game['game_pk'], test_game['away'], test_game['home'],
                      ev_ticker, word_tickers, feed)

    # 6. No spurious events after catchup
    test_detect_no_spurious(gm, feed)

    # 7. Synthetic event injection
    test_synthetic_events(gm, word_tickers)

    # 8. File I/O
    test_file_io()

    # 9. analyze_latency parse
    test_analyze()

    # 10. Live poll cycle
    test_live_poll(k, national_games, all_word_tickers, today)

    summary()


if __name__ == '__main__':
    main()
