"""
analyze_latency.py — post-game analysis of MLB API -> Kalshi price lag.

Reads data/latency_events.jsonl and data/latency_prices.csv (written by
collect_latency_data.py).

collect_latency_data.py records EVERY detected game event regardless of
whether it maps to a Kalshi word, and polls ALL active markets every ~10s
regardless of which event just happened. This script uses that fully: for
every (event_type, market) pair seen anywhere in the data, it measures how
much the market's mid-price moved after that event type, compares the move
to the market's own ambient noise level (how much it jitters with no news
at all), and ranks pairs by statistical confidence x economic size. That
surfaces both the "obvious" pairs (e.g. Wild Pitch -> wild pitch market)
and hidden ones (e.g. does a Mound Visit move the pitch clock market?)
on equal footing.

Usage:
  /root/baseball-env/bin/python analyze_latency.py [options]

  --min-n N     minimum sample size for a pair to be ranked (default 4)
  --top N       how many rows to print per ranked table (default 25)
  --buffer S    seconds of exclusion around any event when sampling the
                noise floor (default 90)
  --stride N    sample every Nth price row when building the noise floor,
                for speed (default 6 -> ~every 60s)
  --verbose     also print the old per-event / per-event cross-market dump
"""

import argparse, csv, json, math, os, sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from statistics import pstdev
from typing import Dict, List, Optional, Tuple

DATA_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
EVENTS_FILE = os.path.join(DATA_DIR, 'latency_events.jsonl')
PRICES_FILE = os.path.join(DATA_DIR, 'latency_prices.csv')

ET = timezone(timedelta(hours=-4))

TEST_GAME_PK = 9999  # synthetic rows injected by test_pipeline.py

OFFSETS_S = [10, 20, 30, 60, 120, 300]
MAX_GAP_S = 20  # a price row must be within this of a target offset to count


# -- Loaders ------------------------------------------------------------------

def load_events(path: str) -> List[Dict]:
    if not os.path.exists(path):
        print(f'ERROR: {path} not found — run collect_latency_data.py first')
        sys.exit(1)
    events = []
    with open(path) as f:
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
            events.append(ev)
    events.sort(key=lambda e: e.get('detected_ts', 0))
    return events


def load_prices(path: str) -> List[Dict]:
    if not os.path.exists(path):
        print(f'ERROR: {path} not found — run collect_latency_data.py first')
        sys.exit(1)
    rows = []
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                game_pk = int(row.get('game_pk') or 0)
                if game_pk == TEST_GAME_PK:
                    continue
                ts  = float(row['ts'])
                mid = float(row['mid']) if row.get('mid') else None
                bid = float(row['bid']) if row.get('bid') else None
                ask = float(row['ask']) if row.get('ask') else None
                rows.append({
                    'ts': ts, 'game_pk': game_pk,
                    'word': row.get('word', ''),
                    'bid': bid, 'ask': ask, 'mid': mid,
                })
            except (ValueError, KeyError):
                pass
    rows.sort(key=lambda r: r['ts'])
    return rows


# -- Price series index (bisect-backed for speed) ------------------------------

class PriceIndex:
    """Per (game_pk, word) sorted price series with O(log n) lookups."""

    def __init__(self, prices: List[Dict]):
        by_key: Dict[Tuple, List[Dict]] = defaultdict(list)
        for r in prices:
            by_key[(r['game_pk'], r['word'])].append(r)
        self.series: Dict[Tuple, List[Dict]] = {}
        self.ts_list: Dict[Tuple, List[float]] = {}
        for key, rows in by_key.items():
            rows.sort(key=lambda r: r['ts'])
            self.series[key] = rows
            self.ts_list[key] = [r['ts'] for r in rows]
        self.words = sorted(set(k[1] for k in by_key if k[1]))
        self.game_pks = sorted(set(k[0] for k in by_key))

    def has(self, game_pk, word) -> bool:
        return (game_pk, word) in self.series

    def before(self, game_pk, word, ts) -> Optional[Dict]:
        key = (game_pk, word)
        tl = self.ts_list.get(key)
        if not tl:
            return None
        i = bisect_right(tl, ts) - 1
        return self.series[key][i] if i >= 0 else None

    def near_offset(self, game_pk, word, anchor_ts, offset_s) -> Optional[Dict]:
        key = (game_pk, word)
        tl = self.ts_list.get(key)
        if not tl:
            return None
        target = anchor_ts + offset_s
        i = bisect_left(tl, target)
        candidates = [j for j in (i - 1, i) if 0 <= j < len(tl)]
        if not candidates:
            return None
        best = min(candidates, key=lambda j: abs(tl[j] - target))
        if abs(tl[best] - target) > MAX_GAP_S:
            return None
        return self.series[key][best]


def _mid(row: Optional[Dict]) -> Optional[float]:
    return row['mid'] if row else None


def _fmt(v: Optional[float], width=7) -> str:
    return f'{v:.3f}'.rjust(width) if v is not None else '—'.rjust(width)


# -- Noise floor: how much does a market move with no news at all? ------------

def compute_noise_floor(idx: PriceIndex, events: List[Dict],
                         buffer_s: float, stride: int) -> Dict[str, Dict]:
    """
    For each market word, sample anchor points that are NOT within buffer_s
    of any detected event (any type), and measure the |delta| from anchor to
    anchor+30s / anchor+60s. That's the market's ambient jitter — spread
    noise, unrelated order flow, etc. — against which real event-driven
    moves need to be compared.

    Anchors are restricted to each game's live window (first detected event
    to last), not the raw price series. Some early collection runs polled a
    fixed pre-game/post-game block regardless of actual first pitch, so the
    raw series can contain hours of dead pre-game quotes — including that
    idle time here would understate ambient in-game jitter and make every
    event-driven move look more significant than it is.
    """
    events_by_game: Dict[int, List[float]] = defaultdict(list)
    for e in events:
        events_by_game[e['game_pk']].append(e.get('detected_ts', 0))
    live_window: Dict[int, Tuple[float, float]] = {}
    for g, ts_list in events_by_game.items():
        ts_list.sort()
        live_window[g] = (ts_list[0], ts_list[-1])
    for g in events_by_game:
        events_by_game[g].sort()

    def near_any_event(game_pk, ts) -> bool:
        ev_ts = events_by_game.get(game_pk, [])
        if not ev_ts:
            return False
        i = bisect_left(ev_ts, ts)
        for j in (i - 1, i):
            if 0 <= j < len(ev_ts) and abs(ev_ts[j] - ts) <= buffer_s:
                return True
        return False

    abs_deltas_30: Dict[str, List[float]] = defaultdict(list)
    abs_deltas_60: Dict[str, List[float]] = defaultdict(list)

    for (game_pk, word), rows in idx.series.items():
        if not word:
            continue
        win = live_window.get(game_pk)
        if not win:
            continue
        for i in range(0, len(rows), max(1, stride)):
            anchor = rows[i]
            if anchor['mid'] is None:
                continue
            if not (win[0] <= anchor['ts'] <= win[1]):
                continue  # outside the game's live window — likely idle pre/post-game polling
            if near_any_event(game_pk, anchor['ts']):
                continue
            r30 = idx.near_offset(game_pk, word, anchor['ts'], 30)
            r60 = idx.near_offset(game_pk, word, anchor['ts'], 60)
            if r30 and r30['mid'] is not None:
                abs_deltas_30[word].append(abs(r30['mid'] - anchor['mid']))
            if r60 and r60['mid'] is not None:
                abs_deltas_60[word].append(abs(r60['mid'] - anchor['mid']))

    baseline = {}
    for word in idx.words:
        d30 = abs_deltas_30.get(word, [])
        d60 = abs_deltas_60.get(word, [])
        baseline[word] = {
            'n':          len(d30),
            'mean_abs30': sum(d30) / len(d30) if d30 else 0.0,
            'std30':      pstdev(d30) if len(d30) > 1 else 0.0,
            'mean_abs60': sum(d60) / len(d60) if d60 else 0.0,
        }
    return baseline


# -- Cross-tab: every event type x every market --------------------------------

def build_pairs(events: List[Dict], idx: PriceIndex) -> Dict[Tuple[str, str], List[Dict]]:
    pairs: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)

    for ev in events:
        game_pk  = ev.get('game_pk')
        ev_ts    = ev.get('detected_ts', 0)
        ev_type  = ev.get('event_type') or '?'
        ev_word  = ev.get('word')
        inning   = ev.get('inning') or 0

        for word in idx.words:
            if not idx.has(game_pk, word):
                continue
            pre = idx.before(game_pk, word, ev_ts)
            if pre is None or pre['mid'] is None:
                continue
            r30 = idx.near_offset(game_pk, word, ev_ts, 30)
            r60 = idx.near_offset(game_pk, word, ev_ts, 60)
            if r30 is None and r60 is None:
                continue

            delta30 = (r30['mid'] - pre['mid']) if (r30 and r30['mid'] is not None) else None
            delta60 = (r60['mid'] - pre['mid']) if (r60 and r60['mid'] is not None) else None

            # Lag: seconds until price crosses 30% of its eventual (300s) move,
            # only meaningful if that eventual move is non-trivial.
            end = idx.near_offset(game_pk, word, ev_ts, 300)
            lag = None
            if end and end['mid'] is not None:
                total_move = end['mid'] - pre['mid']
                if abs(total_move) > 0.02:
                    threshold = pre['mid'] + 0.3 * total_move
                    for off in OFFSETS_S:
                        row = idx.near_offset(game_pk, word, ev_ts, off)
                        if row and row['mid'] is not None:
                            if (total_move > 0 and row['mid'] >= threshold) or \
                               (total_move < 0 and row['mid'] <= threshold):
                                lag = off
                                break

            pairs[(ev_type, word)].append({
                'game_pk': game_pk, 'ts': ev_ts, 'inning': inning,
                'delta30': delta30, 'delta60': delta60, 'lag': lag,
                'expected': (word == ev_word),
            })

    return pairs


def pair_stats(samples: List[Dict], baseline: Dict[str, Dict], word: str) -> Optional[Dict]:
    d30 = [s['delta30'] for s in samples if s['delta30'] is not None]
    if not d30:
        return None
    n = len(d30)
    mean_delta = sum(d30) / n
    mean_abs   = sum(abs(x) for x in d30) / n
    std        = pstdev(d30) if n > 1 else 0.0
    se         = std / math.sqrt(n) if n > 1 else 0.0
    z          = (mean_delta / se) if se > 0 else None

    pos = sum(1 for x in d30 if x > 0)
    neg = sum(1 for x in d30 if x < 0)
    moved = pos + neg
    # Fraction of *moves* (excluding exact-zero polls) that share the majority
    # direction — a market that mostly sits still between polls shouldn't be
    # scored as "inconsistent" just because most samples are literally 0.
    consistency = max(pos, neg) / moved if moved else 0.0
    moved_frac = moved / n if n else 0.0

    bl = baseline.get(word, {'mean_abs30': 0.0})
    noise_ratio = (mean_abs / bl['mean_abs30']) if bl['mean_abs30'] > 0 else float('inf')

    lags = [s['lag'] for s in samples if s['lag'] is not None]
    avg_lag = sum(lags) / len(lags) if lags else None

    expected = any(s['expected'] for s in samples)

    score = (abs(z) if z is not None else 0.0) * min(1.0, noise_ratio / 2.0)

    return {
        'n': n, 'mean_delta': mean_delta, 'mean_abs': mean_abs, 'std': std,
        'z': z, 'consistency': consistency, 'moved_frac': moved_frac,
        'noise_ratio': noise_ratio,
        'avg_lag': avg_lag, 'n_lag': len(lags), 'expected': expected,
        'score': score,
    }


# -- Printing -------------------------------------------------------------------

def print_noise_floor(baseline: Dict[str, Dict]) -> None:
    print('\n' + '-' * 88)
    print('NOISE FLOOR — ambient price jitter per market with no nearby event')
    print('-' * 88)
    print('Any event-driven move needs to clear this bar to be a real signal, not spread noise.')
    print(f'  {"Market":<18} {"n_anchors":>9} {"mean|Δ30s|":>11} {"mean|Δ60s|":>11} {"std30":>8}')
    for word in sorted(baseline, key=lambda w: -baseline[w]['mean_abs30']):
        b = baseline[word]
        if b['n'] == 0:
            continue
        print(f'  {word:<18} {b["n"]:>9} {b["mean_abs30"]:>11.4f} '
              f'{b["mean_abs60"]:>11.4f} {b["std30"]:>8.4f}')


def print_ranked(title: str, rows: List[Tuple[Tuple[str, str], Dict]], top: int) -> None:
    print('\n' + '-' * 100)
    print(title)
    print('-' * 100)
    print(f'  {"Event type":<24} {"Market":<15} {"N":>4} {"avg Δ30s":>9} '
          f'{"|Δ|":>7} {"noise×":>7} {"moved%":>7} {"agree%":>7} {"z":>6} {"lag":>6} {"score":>7}')
    for (ev_type, word), st in rows[:top]:
        lag_str = f'{st["avg_lag"]:.0f}s' if st['avg_lag'] is not None else '—'
        z_str = f'{st["z"]:.1f}' if st['z'] is not None else '—'
        print(f'  {ev_type:<24} {word:<15} {st["n"]:>4} {st["mean_delta"]:>+9.3f} '
              f'{st["mean_abs"]:>7.3f} {st["noise_ratio"]:>6.1f}x {st["moved_frac"]*100:>6.0f}% '
              f'{st["consistency"]*100:>6.0f}% {z_str:>6} {lag_str:>6} {st["score"]:>7.2f}')


def print_verbose_dump(events: List[Dict], idx: PriceIndex) -> None:
    print('\n' + '-' * 80)
    print('PER-EVENT DETAIL (--verbose)')
    print('-' * 80)
    for ev in events:
        game_pk = ev.get('game_pk')
        word    = ev.get('word')
        ev_ts   = ev.get('detected_ts', 0)
        ts_str  = datetime.fromtimestamp(ev_ts, tz=ET).strftime('%H:%M:%S')
        print(f'\n  [{ts_str}] {ev.get("away")}@{ev.get("home")} inn={ev.get("inning")} '
              f'{ev.get("event_type")} -> {word}')
        print(f'  {ev.get("description","")[:90]}')
        if word:
            pre = idx.before(game_pk, word, ev_ts)
            line = [f'pre={_fmt(_mid(pre), 6).strip()}']
            for off in OFFSETS_S:
                row = idx.near_offset(game_pk, word, ev_ts, off)
                line.append(f'T+{off}s={_fmt(_mid(row), 6).strip()}')
            print('  ' + '  '.join(line))


# -- Main -------------------------------------------------------------------

def analyze(events: List[Dict], prices: List[Dict], min_n: int, top: int,
            buffer_s: float, stride: int, verbose: bool) -> None:
    if not events:
        print('No real events to analyze (only test data found, or file is empty).')
        return

    idx = PriceIndex(prices)

    print('\n' + '=' * 100)
    print('LATENCY ANALYSIS — every event type vs. every market, scored against ambient noise')
    print('=' * 100)
    print(f'Events: {len(events)}   Price rows: {len(prices)}   '
          f'Games: {len(idx.game_pks)}   Markets: {len(idx.words)}')

    print('\nComputing noise floor (ambient jitter per market away from any event)...')
    baseline = compute_noise_floor(idx, events, buffer_s, stride)

    print('Building event-type x market cross-tab...')
    pairs = build_pairs(events, idx)

    stats: Dict[Tuple[str, str], Dict] = {}
    for key, samples in pairs.items():
        ev_type, word = key
        st = pair_stats(samples, baseline, word)
        if st and st['n'] >= min_n:
            stats[key] = st

    print_noise_floor(baseline)

    expected_rows = sorted(
        [(k, v) for k, v in stats.items() if v['expected']],
        key=lambda kv: -kv[1]['score'])
    hidden_rows = sorted(
        [(k, v) for k, v in stats.items() if not v['expected']],
        key=lambda kv: -kv[1]['score'])

    print_ranked(
        f'KNOWN PAIRS — the event type this word is expected to react to '
        f'(N >= {min_n}, ranked by score)',
        expected_rows, top)

    print_ranked(
        f'HIDDEN OPPORTUNITIES — every other event type x market combination '
        f'(N >= {min_n}, ranked by score)',
        hidden_rows, top)

    # -- Trend cut: does the reaction change over the course of the game? -----
    print('\n' + '-' * 100)
    print(f'TREND BY INNING — top {min(5, len(hidden_rows) + len(expected_rows))} pairs by score, '
          f'split early (inn 1-3) / mid (4-6) / late (7+)')
    print('-' * 100)
    top_pairs = sorted(list(stats.items()), key=lambda kv: -kv[1]['score'])[:5]
    for key, st in top_pairs:
        ev_type, word = key
        samples = pairs[key]
        buckets = {'early (1-3)': [], 'mid (4-6)': [], 'late (7+)': []}
        for s in samples:
            if s['delta30'] is None:
                continue
            inn = s['inning'] or 0
            b = 'early (1-3)' if inn <= 3 else ('mid (4-6)' if inn <= 6 else 'late (7+)')
            buckets[b].append(abs(s['delta30']))
        tag = 'expected' if st['expected'] else 'hidden'
        print(f'\n  {ev_type} -> {word}  ({tag}, overall N={st["n"]}, score={st["score"]:.2f})')
        for b, vals in buckets.items():
            if vals:
                print(f'    {b:<14} n={len(vals):>3}  mean|Δ30s|={sum(vals)/len(vals):.3f}')
            else:
                print(f'    {b:<14} n=0')

    print()
    print('  score       = |z-score of mean Δ30s| x min(1, noise_ratio/2)')
    print('                z-score: is the average move different from zero given its variance?')
    print('                noise_ratio: how many multiples of ambient jitter the average |move| is.')
    print('  moved%      = fraction of samples where price changed at all between polls')
    print('  agree%      = of the samples that moved, fraction that moved the same direction')
    print('  lag         = avg seconds until price crossed 30% of its eventual (300s) move,')
    print('                only counted when that eventual move exceeds noise (>0.02)')
    print(f'  min_n={min_n} — pairs with fewer samples are excluded from ranking; raise --top to see more.')
    print('  CAVEAT: common event types (Strikeout, Groundout, Batter Timeout, ...) occur every')
    print('          ~1-2 minutes, so their 30/60s windows overlap heavily within one game. N and z')
    print('          overstate independent evidence for those — treat high-N, low-|Δ| hidden rows as')
    print('          "probably game-phase drift", not a real reaction, unless |Δ| clears noise by 2x+.')

    if verbose:
        print_verbose_dump(events, idx)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--events', default=EVENTS_FILE)
    ap.add_argument('--prices', default=PRICES_FILE)
    ap.add_argument('--min-n', type=int, default=4)
    ap.add_argument('--top', type=int, default=25)
    ap.add_argument('--buffer', type=float, default=90.0)
    ap.add_argument('--stride', type=int, default=6)
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    events = load_events(args.events)
    prices = load_prices(args.prices)
    analyze(events, prices, args.min_n, args.top, args.buffer, args.stride, args.verbose)


if __name__ == '__main__':
    main()
