"""
check_results.py — show open positions, settled P&L, and fill history
for all KXMLBMENTION bets. Reads the Kalshi portfolio API directly.

Usage:
    python check_results.py               # open + settled + fills
    python check_results.py --open        # open positions only
    python check_results.py --settled     # settled + fill history only
"""

import argparse
import os

from kalshi import KalshiClient
from utils import TICKER_SUFFIX_TO_WORD

KALSHI_KEY_ID   = os.getenv('KALSHI_KEY_ID',  '')
KALSHI_KEY_PATH = os.getenv('KALSHI_KEY_PATH', '~/.ssh/kalshi.pem')


def parse_ticker(ticker):
    """Return (game_code, word) from a KXMLBMENTION market ticker."""
    parts = ticker.split('-')
    if len(parts) < 3 or parts[0] != 'KXMLBMENTION':
        return ticker, '?'
    game_code = parts[1]   # e.g. 26MAY03TORMIN
    suffix    = parts[2]   # e.g. WALK
    word      = TICKER_SUFFIX_TO_WORD.get(suffix, suffix.lower())
    return game_code, word


def fmt_pnl(v):
    try:
        f = float(v)
        return f'+${f:.2f}' if f >= 0 else f'-${abs(f):.2f}'
    except (TypeError, ValueError):
        return str(v)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--open',    action='store_true')
    parser.add_argument('--settled', action='store_true')
    args = parser.parse_args()

    if not KALSHI_KEY_ID:
        raise SystemExit('Set KALSHI_KEY_ID and KALSHI_KEY_PATH env vars.')

    # show_open / show_settled both default to True when neither flag given
    show_open    = args.open    or (not args.open and not args.settled)
    show_settled = args.settled or (not args.open and not args.settled)

    client  = KalshiClient(KALSHI_KEY_ID, KALSHI_KEY_PATH)
    balance = client.get_balance()
    print(f'\nBalance: ${balance:.2f}\n')

    # ── Open positions ────────────────────────────────────────────────────────
    if show_open:
        positions = client.get_positions()
        kxm = [p for p in positions
               if 'KXMLBMENTION' in p.get('ticker', '')
               and float(p.get('position_fp', 0)) != 0]

        print('── Open positions ───────────────────────────────────────────────')
        if kxm:
            print(f'  {"Ticker":<45} {"Pos":>5} {"Cost":>8}  Word')
            print('  ' + '─' * 70)
            for p in sorted(kxm, key=lambda x: x['ticker']):
                _, word = parse_ticker(p['ticker'])
                pos  = float(p['position_fp'])
                cost = float(p.get('total_traded_dollars', 0))
                side = 'YES' if pos > 0 else 'NO '
                print(f'  {p["ticker"]:<45} {side} {abs(pos):>2.0f}x  ${cost:>6.2f}  {word}')
            print(f'\n  {len(kxm)} open  |  total cost: ${sum(float(p.get("total_traded_dollars",0)) for p in kxm):.2f}')
        else:
            print('  (none)')

    # ── Settled history ───────────────────────────────────────────────────────
    if show_settled:
        settlements = client.get_settlements()
        kxm_s = [s for s in settlements if 'KXMLBMENTION' in s.get('ticker', '')]

        print()
        print('── Settled bets ─────────────────────────────────────────────────')
        if kxm_s:
            print(f'  {"Ticker":<45} {"Side":>4} {"Qty":>3} {"Cost":>7} {"Result":<6} {"Pay":>6} {"P&L":>8}  Word')
            print('  ' + '─' * 95)
            total_pnl = 0.0
            for s in sorted(kxm_s, key=lambda x: x.get('settled_time', '')):
                ticker  = s.get('ticker', '')
                _, word = parse_ticker(ticker)
                result  = s.get('market_result', '')
                yes_qty = float(s.get('yes_count_fp', 0))
                no_qty  = float(s.get('no_count_fp', 0))
                yes_cost = float(s.get('yes_total_cost_dollars', 0))
                no_cost  = float(s.get('no_total_cost_dollars', 0))
                fee      = float(s.get('fee_cost', 0))

                if yes_qty > 0 and no_qty == 0:
                    side, qty, cost = 'YES', yes_qty, yes_cost
                elif no_qty > 0 and yes_qty == 0:
                    side, qty, cost = 'NO', no_qty, no_cost
                else:
                    side, qty, cost = '?', yes_qty + no_qty, yes_cost + no_cost

                payout = yes_qty if result == 'yes' else no_qty
                profit = payout - cost - fee
                total_pnl += profit
                print(f'  {ticker:<45} {side:>4} {qty:>3.0f} '
                      f'${cost:>6.2f} {result:<6} ${payout:>5.2f} {fmt_pnl(profit):>8}  {word}')
            print(f'\n  {len(kxm_s)} settled  |  net P&L: {fmt_pnl(total_pnl)}')
        else:
            print('  (none yet — appears after market finalization)')

    # ── Fill history ──────────────────────────────────────────────────────────
    if show_settled:
        fills  = client.get_fills()
        kxm_f  = [f for f in fills if 'KXMLBMENTION' in f.get('ticker', '')]

        if kxm_f:
            print()
            print('── Fill history ─────────────────────────────────────────────────')
            print(f'  {"Date":<12} {"Ticker":<40} {"Side":<4} {"Qty":>3} {"Price":>7} {"Cost":>7}  Word')
            print('  ' + '─' * 85)
            total_spent = 0.0
            by_event    = {}

            for f in sorted(kxm_f, key=lambda x: x.get('created_time', '')):
                _, word = parse_ticker(f.get('ticker', ''))
                side    = f.get('side', '?').upper()
                qty     = float(f.get('count_fp', 0))
                price_key = 'yes_price_dollars' if side == 'YES' else 'no_price_dollars'
                price   = float(f.get(price_key, 0))
                cost    = float(f.get('taker_fill_cost_dollars',
                                f.get('maker_fill_cost_dollars', qty * price)))
                fees    = float(f.get('fee_cost', 0))
                dt      = f.get('created_time', '')[:10]
                game_code, _ = parse_ticker(f.get('ticker', ''))

                total_spent += cost + fees
                by_event.setdefault(game_code, []).append((word, side, qty, cost, fees))

                print(f'  {dt:<12} {f["ticker"]:<40} {side:<4} {qty:>3.0f} '
                      f'${price:>5.2f}  ${cost:>5.2f}  {word}')

            print(f'\n  Total spent (cost + fees): ${total_spent:.2f}')

            # Per-event cost summary
            print()
            print('── Per-event cost summary ───────────────────────────────────────')
            for ev, bets in sorted(by_event.items()):
                ev_cost = sum(c + fe for _, _, _, c, fe in bets)
                bet_str = ', '.join(f'{side[:1]}{w[:4]}' for w, side, _, _, _ in bets)
                print(f'  {ev:<22}  ${ev_cost:>6.2f}  ({bet_str})')


if __name__ == '__main__':
    main()
