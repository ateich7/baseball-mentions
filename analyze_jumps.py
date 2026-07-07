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
from datetime import date
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
        'entry_costs': [], 'taker_payoffs': [], 'maker_payoffs': [],
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
            correct = (implied == outcome)
            if correct:
                m['n_correct'] += 1

            # Taker entry cost: what you'd actually pay right now to take this
            # side — yes_ask on the high side, (1 - yes_bid) for a no on the
            # low side (Kalshi's implied no-price). Falls back to mid if the
            # book side is missing.
            if side == 'high':
                taker_cost = cross['ask'] if cross.get('ask') is not None else cross['mid']
                maker_cost = cross['mid']
            else:
                taker_cost = (1 - cross['bid']) if cross.get('bid') is not None else (1 - cross['mid'])
                maker_cost = 1 - cross['mid']

            m['entry_costs'].append(taker_cost)
            m['taker_payoffs'].append((1 - taker_cost) if correct else -taker_cost)
            m['maker_payoffs'].append((1 - maker_cost) if correct else -maker_cost)

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


TAKER_FEE = 0.07  # fraction of profit, per CLAUDE.md


def print_gap_report(per_market: Dict[str, Dict], hi: float, lo: float) -> None:
    print('\n' + '=' * 100)
    print(f'GAP AFTER CROSSING — is there still money on the table right after price hits {hi:.2f}/{lo:.2f}?')
    print('=' * 100)
    print('Realized $ per $1 wagered, entering the instant price crosses the threshold.')
    print(f'  {"Market":<15} {"n":>4} {"avg entry":>10} {"taker (pre-fee)":>16} '
          f'{"taker (after 7% fee)":>21} {"maker (fee-free)":>17}')
    print('  ' + '-' * 88)

    for word in sorted(per_market, key=lambda w: -len(per_market[w]['taker_payoffs'])):
        m = per_market[word]
        n = len(m['taker_payoffs'])
        if n == 0:
            continue
        avg_entry = sum(m['entry_costs']) / n
        avg_taker = sum(m['taker_payoffs']) / n
        avg_taker_fee = sum(p * (1 - TAKER_FEE) if p > 0 else p for p in m['taker_payoffs']) / n
        avg_maker = sum(m['maker_payoffs']) / n
        print(f'  {word:<15} {n:>4} {avg_entry:>+10.3f} {avg_taker:>+16.3f} '
              f'{avg_taker_fee:>+21.3f} {avg_maker:>+17.3f}')

    print()
    print('  avg entry     = avg price paid to take the implied side right at the crossing')
    print('                  (yes_ask on the high side, implied no-ask = 1-yes_bid on the low side)')
    print('  taker         = avg profit/loss per $1 risked, crossing the spread to enter immediately')
    print('  maker         = same, but assuming a fee-free fill at the pre-crossing mid instead of')
    print('                  crossing the spread — the optimistic case if you can post ahead of the move')
    print('  Positive taker numbers mean real edge survives paying the spread; positive maker-only')
    print('  numbers mean the edge exists but the spread is currently eating it — worth trying to')
    print('  quote inside the market rather than taking, not worth chasing at the ask.')


def sweep_thresholds(events: List[Dict], prices: List[Dict], outcomes: Dict[Tuple[int, str], int],
                      buffer_s: float, thresholds: List[Tuple[float, float]]) -> List[Dict]:
    rows = []
    for hi, lo in thresholds:
        per_market = analyze(events, prices, outcomes, hi, lo, buffer_s)
        n_series = sum(m['n_series'] for m in per_market.values())
        n_crossed = sum(m['n_crossed'] for m in per_market.values())
        n_with_outcome = sum(m['n_with_outcome'] for m in per_market.values())
        n_correct = sum(m['n_correct'] for m in per_market.values())
        n_stuck = sum(m['n_stuck'] for m in per_market.values())
        all_skip = [f for m in per_market.values() for f in m['skip_fracs']]
        rows.append({
            'hi': hi, 'lo': lo,
            'n_series': n_series, 'n_crossed': n_crossed,
            'cross_pct': 100 * n_crossed / n_series if n_series else 0,
            'accuracy': 100 * n_correct / n_with_outcome if n_with_outcome else None,
            'n_with_outcome': n_with_outcome,
            'stuck_pct': 100 * n_stuck / n_crossed if n_crossed else 0,
            'skip_pct': 100 * sum(all_skip) / len(all_skip) if all_skip else 0,
        })
    return rows


def print_sweep_report(rows: List[Dict]) -> None:
    print('\n' + '=' * 100)
    print('THRESHOLD SWEEP — how far can the crossing range widen before accuracy degrades?')
    print('(pooled across all markets)')
    print('=' * 100)
    print(f'  {"hi/lo":>11} {"crossed":>8} {"cross%":>7} {"accuracy":>16} {"stuck%":>7} {"avg skip%":>10}')
    print('  ' + '-' * 66)
    for r in rows:
        acc = f'{r["accuracy"]:.0f}% (n={r["n_with_outcome"]})' if r['accuracy'] is not None else '—'
        print(f'  {r["hi"]:.2f}/{r["lo"]:.2f}  {r["n_crossed"]:>8} {r["cross_pct"]:>6.0f}% '
              f'{acc:>16} {r["stuck_pct"]:>6.0f}% {r["skip_pct"]:>9.0f}%')
    print()
    print('  Widening the range (moving toward 0.50/0.50) trades accuracy for coverage — more')
    print('  crossings and more skip time, but each crossing is a weaker signal. Narrowing toward')
    print('  0.99/0.01 does the opposite: fewer, near-certain crossings, less time saved.')


HISTORY_FILE = os.path.join(DATA_DIR, 'opt_out_history.csv')
HISTORY_COLS = [
    'run_date', 'hi', 'lo', 'market', 'n_series', 'n_crossed', 'cross_pct',
    'accuracy_pct', 'n_with_outcome', 'stuck_pct', 'explained_pct', 'skip_pct',
    'avg_entry', 'taker_pnl', 'taker_pnl_after_fee', 'maker_pnl',
]


def log_history(per_market: Dict[str, Dict], hi: float, lo: float, run_date: str) -> None:
    """
    Append one row per market to data/opt_out_history.csv so results from
    today are still comparable after more games have been collected — the
    printed report alone doesn't persist anywhere once the terminal scrolls.
    Server-only, append-only, not git-tracked (same treatment as the other
    latency_*.csv data files).
    """
    need_header = not os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_COLS)
        if need_header:
            w.writeheader()
        for word, m in per_market.items():
            if m['n_crossed'] == 0:
                continue
            n_gap = len(m['taker_payoffs'])
            w.writerow({
                'run_date': run_date, 'hi': hi, 'lo': lo, 'market': word,
                'n_series': m['n_series'], 'n_crossed': m['n_crossed'],
                'cross_pct': round(100 * m['n_crossed'] / m['n_series'], 1) if m['n_series'] else '',
                'accuracy_pct': round(100 * m['n_correct'] / m['n_with_outcome'], 1) if m['n_with_outcome'] else '',
                'n_with_outcome': m['n_with_outcome'],
                'stuck_pct': round(100 * m['n_stuck'] / m['n_crossed'], 1),
                'explained_pct': round(100 * m['n_explained'] / m['n_crossed'], 1),
                'skip_pct': round(100 * sum(m['skip_fracs']) / len(m['skip_fracs']), 1) if m['skip_fracs'] else '',
                'avg_entry': round(sum(m['entry_costs']) / n_gap, 4) if n_gap else '',
                'taker_pnl': round(sum(m['taker_payoffs']) / n_gap, 4) if n_gap else '',
                'taker_pnl_after_fee': round(sum(
                    p * (1 - TAKER_FEE) if p > 0 else p for p in m['taker_payoffs']) / n_gap, 4) if n_gap else '',
                'maker_pnl': round(sum(m['maker_payoffs']) / n_gap, 4) if n_gap else '',
            })
    print(f'\nAppended {sum(1 for m in per_market.values() if m["n_crossed"] > 0)} rows to {HISTORY_FILE}')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--events', default=EVENTS_FILE)
    ap.add_argument('--prices', default=PRICES_FILE)
    ap.add_argument('--outcomes', default=OUTCOMES_FILE)
    ap.add_argument('--hi', type=float, default=0.90)
    ap.add_argument('--lo', type=float, default=0.10)
    ap.add_argument('--buffer', type=float, default=EXPLAIN_BUFFER_S)
    ap.add_argument('--no-log', action='store_true', help='skip appending to opt_out_history.csv')
    args = ap.parse_args()

    events = load_events(args.events)
    prices = load_prices(args.prices)
    outcomes = load_outcomes(args.outcomes)

    per_market = analyze(events, prices, outcomes, args.hi, args.lo, args.buffer)
    print_report(per_market, args.hi, args.lo)
    print_gap_report(per_market, args.hi, args.lo)

    thresholds = [(0.99, 0.01), (0.97, 0.03), (0.95, 0.05), (0.90, 0.10), (0.85, 0.15),
                  (0.80, 0.20), (0.75, 0.25), (0.70, 0.30), (0.65, 0.35), (0.60, 0.40)]
    sweep_rows = sweep_thresholds(events, prices, outcomes, args.buffer, thresholds)
    print_sweep_report(sweep_rows)

    if not args.no_log:
        log_history(per_market, args.hi, args.lo, date.today().isoformat())


if __name__ == '__main__':
    main()
