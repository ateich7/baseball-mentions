"""
analyze_jumps.py — backtest the "opt out once price is already extreme" rule.

Idea: once a market's price has moved near $1 (word basically already said)
or near $0 (basically ruled out), the question it's tracking is effectively
resolved and there's no more edge left to bet on. This checks that idea
against real data:

  - How often does a market actually reach an extreme before game end?
  - Once it does, does price stay there (rule is safe) or reverse (rule
    would have made you miss a real move)?
  - Does the side it lands on match the real settlement outcome?
  - Does the crossing line up with a logged game event, or is it an
    unexplained liquidity blip?
  - How much of the remaining game would the rule tell you to sit out?

Needs data/latency_outcomes.csv — run fetch_latency_outcomes.py on the
server first (needs Kalshi credentials; this script does not).

Usage:
  /root/baseball-env/bin/python analyze_jumps.py [--hi 0.90] [--lo 0.10]
"""

import argparse, csv, os, sys
from bisect import bisect_left
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from analyze_latency import load_events, load_prices, PriceIndex, EVENTS_FILE, PRICES_FILE

DATA_DIR      = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
OUTCOMES_FILE = os.path.join(DATA_DIR, 'latency_outcomes.csv')

EXPLAIN_BUFFER_S = 90  # window around a crossing to look for a logged event


def load_outcomes(path: str) -> Dict[Tuple[int, str], int]:
    if not os.path.exists(path):
        print(f'ERROR: {path} not found — run fetch_latency_outcomes.py on the server first')
        sys.exit(1)
    out: Dict[Tuple[int, str], int] = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            if row['result'] == '':
                continue
            out[(int(row['game_pk']), row['word'])] = int(row['result'])
    return out


def live_windows(events: List[Dict]) -> Dict[int, Tuple[float, float]]:
    by_game = defaultdict(list)
    for e in events:
        by_game[e['game_pk']].append(e.get('detected_ts', 0))
    return {g: (min(ts), max(ts)) for g, ts in by_game.items() if ts}


def events_by_game_sorted(events: List[Dict]) -> Dict[int, List[float]]:
    by_game = defaultdict(list)
    for e in events:
        by_game[e['game_pk']].append(e.get('detected_ts', 0))
    for g in by_game:
        by_game[g].sort()
    return by_game


def find_first_crossing(rows: List[Dict], win: Tuple[float, float],
                         hi: float, lo: float) -> Optional[Dict]:
    """First row within the live window whose mid is >= hi or <= lo."""
    for r in rows:
        if not (win[0] <= r['ts'] <= win[1]):
            continue
        if r['mid'] is None:
            continue
        if r['mid'] >= hi or r['mid'] <= lo:
            return r
    return None


def stayed_put(rows: List[Dict], win: Tuple[float, float], cross_ts: float, side: str) -> bool:
    """Did price stay on the same side of 0.5 for the rest of the live window?"""
    for r in rows:
        if r['ts'] < cross_ts or r['ts'] > win[1] or r['mid'] is None:
            continue
        if side == 'high' and r['mid'] < 0.5:
            return False
        if side == 'low' and r['mid'] > 0.5:
            return False
    return True


def explained(ev_ts_sorted: List[float], cross_ts: float, buffer_s: float) -> bool:
    if not ev_ts_sorted:
        return False
    i = bisect_left(ev_ts_sorted, cross_ts)
    for j in (i - 1, i):
        if 0 <= j < len(ev_ts_sorted) and abs(ev_ts_sorted[j] - cross_ts) <= buffer_s:
            return True
    return False


def analyze(events: List[Dict], prices: List[Dict], outcomes: Dict[Tuple[int, str], int],
            hi: float, lo: float, buffer_s: float) -> Dict[str, Dict]:
    idx = PriceIndex(prices)
    windows = live_windows(events)
    ev_by_game = events_by_game_sorted(events)

    per_market: Dict[str, Dict] = defaultdict(lambda: {
        'n_series': 0, 'n_crossed': 0, 'n_with_outcome': 0, 'n_correct': 0,
        'n_stuck': 0, 'n_reversed': 0, 'n_explained': 0, 'skip_fracs': [],
    })

    for (game_pk, word), rows in idx.series.items():
        if not word:
            continue
        win = windows.get(game_pk)
        if not win or win[1] <= win[0]:
            continue

        m = per_market[word]
        m['n_series'] += 1

        cross = find_first_crossing(rows, win, hi, lo)
        if cross is None:
            continue
        m['n_crossed'] += 1

        side = 'high' if cross['mid'] >= hi else 'low'
        implied = 1 if side == 'high' else 0

        outcome = outcomes.get((game_pk, word))
        if outcome is not None:
            m['n_with_outcome'] += 1
            if implied == outcome:
                m['n_correct'] += 1

        if stayed_put(rows, win, cross['ts'], side):
            m['n_stuck'] += 1
        else:
            m['n_reversed'] += 1

        if explained(ev_by_game.get(game_pk, []), cross['ts'], buffer_s):
            m['n_explained'] += 1

        m['skip_fracs'].append((win[1] - cross['ts']) / (win[1] - win[0]))

    return per_market


def print_report(per_market: Dict[str, Dict], hi: float, lo: float) -> None:
    print('\n' + '=' * 100)
    print(f'OPT-OUT BACKTEST — once price crosses {hi:.2f} / {lo:.2f}, is the market really done moving?')
    print('=' * 100)
    print(f'  {"Market":<15} {"series":>7} {"crossed":>8} {"cross%":>7} {"accuracy":>9} '
          f'{"stuck%":>7} {"explained%":>11} {"avg skip%":>10}')
    print('  ' + '-' * 88)

    for word in sorted(per_market, key=lambda w: -per_market[w]['n_crossed']):
        m = per_market[word]
        if m['n_crossed'] == 0:
            continue
        cross_pct = 100 * m['n_crossed'] / m['n_series'] if m['n_series'] else 0
        acc = f'{100*m["n_correct"]/m["n_with_outcome"]:.0f}% (n={m["n_with_outcome"]})' \
              if m['n_with_outcome'] else '— (no outcome data)'
        stuck_pct = 100 * m['n_stuck'] / m['n_crossed']
        expl_pct  = 100 * m['n_explained'] / m['n_crossed']
        skip_pct  = 100 * sum(m['skip_fracs']) / len(m['skip_fracs']) if m['skip_fracs'] else 0
        print(f'  {word:<15} {m["n_series"]:>7} {m["n_crossed"]:>8} {cross_pct:>6.0f}% '
              f'{acc:>18} {stuck_pct:>6.0f}% {expl_pct:>10.0f}% {skip_pct:>9.0f}%')

    print()
    print('  series      = (game, market) price series with a usable live window')
    print('  crossed     = how many ever hit the extreme threshold before game end')
    print('  cross%      = crossed / series')
    print('  accuracy    = of crossings with a known settlement, % where the side it landed on')
    print('                matched the real outcome')
    print('  stuck%      = % of crossings where price stayed on that side through game end')
    print('                (rule would have been safe to opt out)')
    print('  explained%  = % of crossings within 90s of a logged game event, vs. an unexplained')
    print('                liquidity blip with no game action behind it')
    print('  avg skip%   = avg share of the remaining game the rule would tell you to sit out')
    print()
    print('  Read together: high stuck% + high accuracy + high explained% = the rule is safe to')
    print('  automate for that market. High cross% + high avg skip% = it also saves real trading time.')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--events', default=EVENTS_FILE)
    ap.add_argument('--prices', default=PRICES_FILE)
    ap.add_argument('--outcomes', default=OUTCOMES_FILE)
    ap.add_argument('--hi', type=float, default=0.90)
    ap.add_argument('--lo', type=float, default=0.10)
    ap.add_argument('--buffer', type=float, default=EXPLAIN_BUFFER_S)
    args = ap.parse_args()

    events = load_events(args.events)
    prices = load_prices(args.prices)
    outcomes = load_outcomes(args.outcomes)
    per_market = analyze(events, prices, outcomes, args.hi, args.lo, args.buffer)
    print_report(per_market, args.hi, args.lo)


if __name__ == '__main__':
    main()
