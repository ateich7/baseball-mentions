"""
settled_review.py — pull all settled KXMLBMENTION bets and compute P&L.
Run on server:
  KALSHI_KEY_ID=... KALSHI_KEY_PATH=... /root/baseball-env/bin/python settled_review.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kalshi import KalshiClient
from utils import TICKER_SUFFIX_TO_WORD

k = KalshiClient(os.environ['KALSHI_KEY_ID'], os.environ['KALSHI_KEY_PATH'])
balance = k.get_balance()
print(f'\nBalance: ${balance:.2f}')

settlements = k.get_settlements()
kxm = [x for x in settlements if 'KXMLBMENTION' in x.get('ticker', '')]
print(f'Settled KXMLBMENTION markets: {len(kxm)}\n')

total_cost = 0.0
total_payout = 0.0
total_fees = 0.0
wins = losses = no_pos = 0

by_word = {}
by_game = {}

print(f'{"Date":<12} {"Game":<18} {"Word":<15} {"Side":<4} {"Qty":>5} {"Cost":>7} {"Result":<6} {"Pay":>6} {"P&L":>8}')
print('-' * 90)

for item in sorted(kxm, key=lambda x: x.get('settled_time', '')):
    ticker = item['ticker']
    parts = ticker.split('-')
    game   = parts[1] if len(parts) >= 2 else '?'
    suffix = parts[2] if len(parts) >= 3 else '?'
    word   = TICKER_SUFFIX_TO_WORD.get(suffix, suffix.lower())

    result   = item['market_result']          # 'yes' or 'no'
    yes_qty  = float(item['yes_count_fp'])
    no_qty   = float(item['no_count_fp'])
    yes_cost = float(item['yes_total_cost_dollars'])
    no_cost  = float(item['no_total_cost_dollars'])
    fee      = float(item['fee_cost'])
    date     = item['settled_time'][:10]

    if yes_qty > 0 and no_qty == 0:
        side = 'YES'
        qty  = yes_qty
        cost = yes_cost
    elif no_qty > 0 and yes_qty == 0:
        side = 'NO'
        qty  = no_qty
        cost = no_cost
    else:
        side = '?'
        qty  = yes_qty + no_qty
        cost = yes_cost + no_cost

    if result == 'yes':
        payout = yes_qty * 1.0
    else:
        payout = no_qty * 1.0

    pnl = payout - cost - fee

    total_cost   += cost + fee
    total_payout += payout
    total_fees   += fee

    if cost == 0:
        no_pos += 1
    elif (side == 'YES' and result == 'yes') or (side == 'NO' and result == 'no'):
        wins += 1
    else:
        losses += 1

    by_word.setdefault(word, {'cost': 0.0, 'payout': 0.0, 'n': 0, 'wins': 0, 'losses': 0})
    by_word[word]['cost']   += cost + fee
    by_word[word]['payout'] += payout
    by_word[word]['n']      += 1
    if cost > 0:
        if (side == 'YES' and result == 'yes') or (side == 'NO' and result == 'no'):
            by_word[word]['wins'] += 1
        else:
            by_word[word]['losses'] += 1

    by_game.setdefault(game, {'cost': 0.0, 'payout': 0.0})
    by_game[game]['cost']   += cost + fee
    by_game[game]['payout'] += payout

    pnl_str = ('+$' if pnl >= 0 else '-$') + f'{abs(pnl):.2f}'
    print(f'{date:<12} {game:<18} {word:<15} {side:<4} {qty:>5.0f} '
          f'${cost:>6.2f} {result:<6} ${payout:>5.2f} {pnl_str:>8}')

net_pnl = total_payout - total_cost
roi = net_pnl / total_cost * 100 if total_cost > 0 else 0.0

print()
print('=' * 60)
print(f'  Settled markets:  {len(kxm)} ({wins}W / {losses}L)')
print(f'  Total wagered:   ${total_cost:.2f}')
print(f'  Total returned:  ${total_payout:.2f}')
print(f'  Fees paid:       ${total_fees:.2f}')
print(f'  Net P&L:         {"+" if net_pnl >= 0 else ""}${net_pnl:.2f}')
print(f'  ROI:             {roi:+.1f}%')
print()

print('── P&L by word ──────────────────────────────────────────')
print(f'  {"Word":<15} {"N":>3} {"W/L":>6} {"Wagered":>8} {"Return":>8} {"P&L":>8} {"ROI":>7}')
print('  ' + '-' * 58)
for w, d in sorted(by_word.items(), key=lambda x: x[1]['payout'] - x[1]['cost'], reverse=True):
    if d['cost'] == 0:
        continue
    pnl = d['payout'] - d['cost']
    roi_w = pnl / d['cost'] * 100
    wl = f"{d['wins']}/{d['losses']}"
    pnl_str = ('+$' if pnl >= 0 else '-$') + f'{abs(pnl):.2f}'
    print(f'  {w:<15} {d["n"]:>3} {wl:>6} ${d["cost"]:>7.2f} ${d["payout"]:>7.2f} {pnl_str:>8} {roi_w:>+6.1f}%')

print()
print('── P&L by game-day ──────────────────────────────────────')
print(f'  {"Game":<20} {"Wagered":>8} {"Return":>8} {"P&L":>8}')
print('  ' + '-' * 46)
for g, d in sorted(by_game.items()):
    pnl = d['payout'] - d['cost']
    pnl_str = ('+$' if pnl >= 0 else '-$') + f'{abs(pnl):.2f}'
    print(f'  {g:<20} ${d["cost"]:>7.2f} ${d["payout"]:>7.2f} {pnl_str:>8}')
