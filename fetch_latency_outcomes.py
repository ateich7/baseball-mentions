"""
fetch_latency_outcomes.py — fetch real Kalshi settlement results for the
games in data/latency_events.jsonl, caching them to data/latency_outcomes.csv.

data/kalshi_outcomes.csv (the old prediction-pipeline ground truth) only
covers March-May 2026 games — no overlap with the June-July latency
collection window. This queries Kalshi directly for the exact event
tickers we collected latency data for, rather than re-scanning all
settled events.

Run on the server only — needs Kalshi credentials:
  KALSHI_KEY_ID=... KALSHI_KEY_PATH=/root/baseball-mentions/kalshi_key.txt \
  /root/baseball-env/bin/python fetch_latency_outcomes.py
"""

import csv, json, os, sys, time
from typing import Dict, Optional

from kalshi import KalshiClient
from utils import TICKER_SUFFIX_TO_WORD

DATA_DIR      = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
EVENTS_FILE   = os.path.join(DATA_DIR, 'latency_events.jsonl')
OUTCOMES_FILE = os.path.join(DATA_DIR, 'latency_outcomes.csv')

KALSHI_KEY_ID   = os.getenv('KALSHI_KEY_ID', '')
KALSHI_KEY_PATH = os.getenv('KALSHI_KEY_PATH', '/root/baseball-mentions/kalshi_key.txt')

TEST_GAME_PK = 9999


def load_event_tickers() -> Dict[str, dict]:
    """Return {event_ticker: {'game_pk', 'away', 'home'}} from the events log."""
    tickers: Dict[str, dict] = {}
    with open(EVENTS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get('game_pk') == TEST_GAME_PK:
                continue
            t = ev.get('kalshi_event_ticker')
            if t and t not in tickers:
                tickers[t] = {
                    'game_pk': ev.get('game_pk'),
                    'away':    ev.get('away'),
                    'home':    ev.get('home'),
                }
    return tickers


def result_to_int(result: str) -> Optional[int]:
    r = (result or '').strip().lower()
    if r == 'yes':
        return 1
    if r == 'no':
        return 0
    return None


def main() -> None:
    if not KALSHI_KEY_ID:
        print('ERROR: KALSHI_KEY_ID and KALSHI_KEY_PATH must be set')
        sys.exit(1)

    tickers = load_event_tickers()
    print(f'Found {len(tickers)} distinct event tickers in latency_events.jsonl')

    k = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH)

    rows = []
    n_settled_games = 0
    for i, (event_ticker, meta) in enumerate(sorted(tickers.items())):
        try:
            markets = k.get_markets_for_event(event_ticker)
        except Exception as e:
            print(f'  [{i+1}/{len(tickers)}] {event_ticker}  ERROR: {e}')
            continue

        n_resolved_here = 0
        for mkt in markets:
            mkt_ticker = mkt.get('ticker', '') or ''
            suffix = mkt_ticker.rsplit('-', 1)[-1] if '-' in mkt_ticker else ''
            word = TICKER_SUFFIX_TO_WORD.get(suffix)
            if not word:
                continue
            result = result_to_int(mkt.get('result', ''))
            if result is not None:
                n_resolved_here += 1
            rows.append({
                'game_pk': meta['game_pk'], 'event_ticker': event_ticker,
                'away': meta['away'], 'home': meta['home'],
                'word': word, 'result': result if result is not None else '',
            })
        n_settled_games += 1 if n_resolved_here > 0 else 0
        print(f'  [{i+1}/{len(tickers)}] {event_ticker}  '
              f'{n_resolved_here}/{len(markets)} markets settled', flush=True)
        time.sleep(0.2)

    with open(OUTCOMES_FILE, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['game_pk', 'event_ticker', 'away', 'home', 'word', 'result'])
        w.writeheader()
        w.writerows(rows)

    print(f'\nWrote {len(rows)} rows ({n_settled_games}/{len(tickers)} games with '
          f'at least one settled market) to {OUTCOMES_FILE}')


if __name__ == '__main__':
    main()
