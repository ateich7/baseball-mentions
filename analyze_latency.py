"""
analyze_latency.py — post-game analysis of MLB API → Kalshi price lag.

Reads data/latency_events.jsonl and data/latency_prices.csv (written by
collect_latency_data.py).

For each detected event the prices CSV is a continuous time series of ALL
market prices sampled every ~10 seconds. This script aligns each event to
that time series to measure how long each market took to reprice.

Key output per event:
  pre_mid    : market price in the poll immediately before the event
  t+0        : price in the poll immediately after the event
  t+10/30/60 : price at approx 10, 30, 60 seconds post-event
  delta      : how much the price moved, and how fast

Usage:
  /root/baseball-env/bin/python analyze_latency.py [--events F] [--prices F]
"""

import argparse, csv, json, os, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

DATA_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
EVENTS_FILE = os.path.join(DATA_DIR, 'latency_events.jsonl')
PRICES_FILE = os.path.join(DATA_DIR, 'latency_prices.csv')

ET = timezone(timedelta(hours=-4))


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_events(path: str) -> List[Dict]:
    if not os.path.exists(path):
        print(f'ERROR: {path} not found — run collect_latency_data.py first')
        sys.exit(1)
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def load_prices(path: str) -> List[Dict]:
    """
    Returns list of rows sorted by ts. Each row:
      {'ts': float, 'game_pk': int, 'word': str, 'ticker': str,
       'bid': float|None, 'ask': float|None, 'mid': float|None}
    """
    if not os.path.exists(path):
        print(f'ERROR: {path} not found — run collect_latency_data.py first')
        sys.exit(1)
    rows = []
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                ts  = float(row['ts'])
                mid = float(row['mid']) if row.get('mid') else None
                bid = float(row['bid']) if row.get('bid') else None
                ask = float(row['ask']) if row.get('ask') else None
                rows.append({
                    'ts':      ts,
                    'game_pk': int(row.get('game_pk') or 0),
                    'away':    row.get('away', ''),
                    'home':    row.get('home', ''),
                    'word':    row.get('word', ''),
                    'ticker':  row.get('ticker', ''),
                    'bid':     bid,
                    'ask':     ask,
                    'mid':     mid,
                })
            except (ValueError, KeyError):
                pass
    rows.sort(key=lambda r: r['ts'])
    return rows


# ── Price lookup helpers ──────────────────────────────────────────────────────

def build_price_index(prices: List[Dict]) -> Dict[Tuple, List[Dict]]:
    """Index prices by (game_pk, word) for fast timestamp lookup."""
    idx = defaultdict(list)
    for r in prices:
        idx[(r['game_pk'], r['word'])].append(r)
    return idx


def price_at(series: List[Dict], ts: float, direction: str = 'after') -> Optional[Dict]:
    """
    Find the price row nearest to ts.
      direction='before' — last row with row['ts'] <= ts
      direction='after'  — first row with row['ts'] >= ts
    """
    if not series:
        return None
    if direction == 'before':
        result = None
        for r in series:
            if r['ts'] <= ts:
                result = r
            else:
                break
        return result
    else:  # after
        for r in series:
            if r['ts'] >= ts:
                return r
        return None


def price_at_offset(series: List[Dict], event_ts: float, offset_s: float) -> Optional[Dict]:
    """Find the price row closest to event_ts + offset_s."""
    target = event_ts + offset_s
    best, best_gap = None, float('inf')
    for r in series:
        gap = abs(r['ts'] - target)
        if gap < best_gap:
            best, best_gap = r, gap
    # Only return if within 2× the poll interval (20s) of the target
    return best if best_gap <= 20 else None


def _mid(row: Optional[Dict]) -> Optional[float]:
    return row['mid'] if row else None


def _fmt(v: Optional[float]) -> str:
    return f'{v:.3f}' if v is not None else '  —  '


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze(events: List[Dict], prices: List[Dict]) -> None:
    idx = build_price_index(prices)

    # Filter test events injected by test_pipeline.py
    events = [e for e in events if e.get('game_pk') != 9999]
    if not events:
        print('No real events to analyze (only test data found).')
        return

    offsets_s = [0, 10, 20, 30, 60, 120, 300]

    print('\n' + '='*80)
    print('LATENCY ANALYSIS — MLB API event detection → Kalshi market repricing')
    print('='*80)
    print(f'Events: {len(events)}    Price rows: {len(prices)}')

    # ── Per-event detail ──────────────────────────────────────────────────────
    print('\n' + '-'*80)
    print('INDIVIDUAL EVENTS')
    print('-'*80)

    all_deltas = defaultdict(list)   # word → list of (delta_at_30s,)
    all_lags   = defaultdict(list)   # word → list of seconds_to_half_move

    for ev in events:
        game_pk  = ev.get('game_pk')
        word     = ev.get('word', '')
        ev_ts    = ev.get('detected_ts', 0)
        series   = idx.get((game_pk, word), [])

        ts_str = datetime.fromtimestamp(ev_ts, tz=ET).strftime('%H:%M:%S')
        print(f'\n  [{ts_str}] {ev.get("away")}@{ev.get("home")}  '
              f'inn={ev.get("inning")}  {ev.get("event_type")} → {word}')
        print(f'  {ev.get("description","")[:80]}')

        pre  = price_at(series, ev_ts, direction='before')
        post = price_at(series, ev_ts, direction='after')
        print(f'  pre_mid={_fmt(_mid(pre))}  '
              f'(poll {round(ev_ts - pre["ts"], 1) if pre else "?"}s before event)')

        price_line = []
        prev_mid = _mid(pre)
        for off in offsets_s:
            row = price_at_offset(series, ev_ts, off)
            m   = _mid(row)
            price_line.append(f'T+{off}s={_fmt(m)}')
            if m is not None and prev_mid is not None:
                delta = m - prev_mid
                all_deltas[word].append((off, delta))

        print('  ' + '  '.join(price_line))

        # Estimate lag: how many seconds until price moved by >30% of total move
        pre_m  = _mid(pre)
        end_m  = _mid(price_at_offset(series, ev_ts, 300))
        if pre_m is not None and end_m is not None and abs(end_m - pre_m) > 0.02:
            total_move = end_m - pre_m
            threshold  = pre_m + 0.3 * total_move
            for off in offsets_s:
                row = price_at_offset(series, ev_ts, off)
                if row and row.get('mid') is not None:
                    if (total_move > 0 and row['mid'] >= threshold) or \
                       (total_move < 0 and row['mid'] <= threshold):
                        all_lags[word].append(off)
                        break

    # ── Cross-market view: all market prices around each event ────────────────
    print('\n' + '-'*80)
    print('ALL MARKETS AT EVENT TIME (cross-market snapshot)')
    print('-'*80)
    print('Shows every market\'s price immediately before and T+30s after each event.')
    print()

    all_words = sorted(set(r['word'] for r in prices))

    for ev in events:
        game_pk = ev.get('game_pk')
        ev_ts   = ev.get('detected_ts', 0)
        ts_str  = datetime.fromtimestamp(ev_ts, tz=ET).strftime('%H:%M:%S')
        print(f'  [{ts_str}] {ev.get("event_type")} → {ev.get("word")}')
        print(f'  {"Market":<20} {"pre":>7} {"T+10s":>7} {"T+30s":>7} {"T+60s":>7} {"moved?":>8}')
        for word in all_words:
            series = idx.get((game_pk, word), [])
            pre    = _mid(price_at(series, ev_ts, 'before'))
            t10    = _mid(price_at_offset(series, ev_ts, 10))
            t30    = _mid(price_at_offset(series, ev_ts, 30))
            t60    = _mid(price_at_offset(series, ev_ts, 60))
            if pre is None and t30 is None:
                continue
            delta = round(t30 - pre, 3) if pre is not None and t30 is not None else None
            flag  = ''
            if delta is not None:
                if abs(delta) >= 0.05:
                    flag = f'  ← {delta:+.3f}'
                elif abs(delta) >= 0.02:
                    flag = f'  ({delta:+.3f})'
            marker = '***' if word == ev.get('word') else '   '
            print(f'  {marker} {word:<17} {_fmt(pre):>7} {_fmt(t10):>7} '
                  f'{_fmt(t30):>7} {_fmt(t60):>7}{flag}')
        print()

    # ── Summary table ─────────────────────────────────────────────────────────
    print('-'*80)
    print('SUMMARY BY WORD')
    print('-'*80)
    print(f'  {"Word":<20} {"N":>3} {"avg_pre":>8} {"avg_T+30":>9} '
          f'{"avg_delta":>10} {"lag_50pct":>10}')
    print('  ' + '-'*64)

    triggered_words = sorted(set(e['word'] for e in events if e.get('word') is not None))
    for word in triggered_words:
        word_events = [e for e in events if e['word'] == word]
        game_pks    = [e['game_pk'] for e in word_events]

        pres, t30s = [], []
        for ev, gpk in zip(word_events, game_pks):
            series = idx.get((gpk, word), [])
            pre    = _mid(price_at(series, ev['detected_ts'], 'before'))
            t30    = _mid(price_at_offset(series, ev['detected_ts'], 30))
            if pre is not None:
                pres.append(pre)
            if t30 is not None:
                t30s.append(t30)

        avg_pre   = sum(pres) / len(pres) if pres else None
        avg_t30   = sum(t30s) / len(t30s) if t30s else None
        avg_delta = (avg_t30 - avg_pre) if avg_pre is not None and avg_t30 is not None else None
        lags      = all_lags.get(word, [])
        avg_lag   = f'{sum(lags)/len(lags):.0f}s' if lags else '—'

        print(f'  {word:<20} {len(word_events):>3} {_fmt(avg_pre):>8} '
              f'{_fmt(avg_t30):>9} {_fmt(avg_delta):>10} {avg_lag:>10}')

    print()
    print('  lag_50pct = avg seconds until price moved 30% of its total shift')
    print('  A short lag means market repriced before we could trade.')
    print('  A lag > 15s suggests a tradeable window exists.')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--events', default=EVENTS_FILE)
    ap.add_argument('--prices', default=PRICES_FILE)
    args = ap.parse_args()

    events = load_events(args.events)
    prices = load_prices(args.prices)
    analyze(events, prices)


if __name__ == '__main__':
    main()
