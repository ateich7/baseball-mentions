"""
add_team_stats.py — pass 4 of the training-data pipeline.

Appends season-to-date team batting + pitching stats (OPS, HR/G, R/G,
ERA, WHIP, K/9) for home and away teams as of the day before each game.
Stats are cached by (team_id, end_date) to avoid redundant API calls.

Overwrites game_labels_final.csv in place.
"""
import csv, time
from datetime import date, timedelta

from utils import get_json, fetch_team_stats

INPUT      = 'game_labels_final.csv'
OUTPUT     = 'game_labels_final.csv'   # overwrite in place
BATCH_SIZE = 50   # games per schedule API call

NEW_COLS = [
    'home_team_id', 'away_team_id',
    'home_ops', 'home_hr_g', 'home_r_g',
    'home_p_era', 'home_p_whip', 'home_p_k9',
    'away_ops', 'away_hr_g', 'away_r_g',
    'away_p_era', 'away_p_whip', 'away_p_k9',
]

# ── Load CSV ──────────────────────────────────────────────────────────────────
with open(INPUT, newline='') as f:
    reader    = csv.DictReader(f)
    orig_cols = reader.fieldnames
    rows      = list(reader)

print(f'Loaded {len(rows)} rows from {INPUT}')

# ── Pass 1: batch-fetch home/away team IDs ────────────────────────────────────
# The schedule endpoint accepts comma-separated gamePks, so we batch to reduce calls.
unique_pks = list(dict.fromkeys(r['game_pk'] for r in rows if r.get('game_pk')))
print(f'Fetching team IDs for {len(unique_pks)} games...')

teams_by_pk = {}
for i in range(0, len(unique_pks), BATCH_SIZE):
    batch = unique_pks[i:i + BATCH_SIZE]
    url   = (f'https://statsapi.mlb.com/api/v1/schedule?sportId=1'
             f'&gamePks={",".join(batch)}&hydrate=team')
    data  = get_json(url)
    if data:
        for day in data.get('dates', []):
            for g in day.get('games', []):
                pk  = str(g.get('gamePk', ''))
                hid = g['teams']['home']['team']['id']
                aid = g['teams']['away']['team']['id']
                teams_by_pk[pk] = (hid, aid)
    print(f'  {min(i+BATCH_SIZE, len(unique_pks))}/{len(unique_pks)}', flush=True)
    time.sleep(0.2)

print(f'Team IDs fetched for {len(teams_by_pk)} games')

# ── Pass 2: team stats cached by (team_id, end_date) ─────────────────────────
# end_date = day before the game (stats "as of" the morning of the game)
stats_cache = {}
work = {}   # pk → (home_id, away_id, season, end_date_str)

for row in rows:
    pk = row.get('game_pk', '')
    if not pk or pk not in teams_by_pk:
        continue
    hid, aid = teams_by_pk[pk]
    try:
        gd     = date.fromisoformat(row['date_raw'])
        season = gd.year
        end    = str(gd - timedelta(days=1))
    except Exception:
        continue
    work[pk] = (hid, aid, season, end)

unique_fetches = {(tid, end, season)
                  for hid, aid, season, end in work.values()
                  for tid in (hid, aid)}
print(f'\nFetching stats for {len(unique_fetches)} unique (team, date) pairs...')

done = 0
for team_id, end_date, season in sorted(unique_fetches):
    key = (team_id, end_date)
    if key not in stats_cache:
        stats_cache[key] = fetch_team_stats(team_id, season, end_date)
        done += 1
        if done % 50 == 0:
            print(f'  {done}/{len(unique_fetches)}', flush=True)

print(f'Done — {done} API pairs called, {len(stats_cache)} cached')

# ── Pass 3: assemble output ───────────────────────────────────────────────────
out_cols = [c for c in orig_cols if c not in NEW_COLS] + NEW_COLS
enriched = []

for row in rows:
    pk    = row.get('game_pk', '')
    empty = {c: '' for c in NEW_COLS}

    if pk in work:
        hid, aid, season, end = work[pk]
        h_ops, h_hr_g, h_r_g, h_era, h_whip, h_k9 = stats_cache.get((hid, end), ('',)*6)
        a_ops, a_hr_g, a_r_g, a_era, a_whip, a_k9 = stats_cache.get((aid, end), ('',)*6)
        row.update({
            'home_team_id': hid,   'away_team_id': aid,
            'home_ops':     h_ops, 'home_hr_g':    h_hr_g, 'home_r_g':  h_r_g,
            'home_p_era':   h_era, 'home_p_whip':  h_whip, 'home_p_k9': h_k9,
            'away_ops':     a_ops, 'away_hr_g':    a_hr_g, 'away_r_g':  a_r_g,
            'away_p_era':   a_era, 'away_p_whip':  a_whip, 'away_p_k9': a_k9,
        })
    else:
        row.update(empty)

    enriched.append(row)

# ── Write ─────────────────────────────────────────────────────────────────────
with open(OUTPUT, 'w', newline='') as fout:
    writer = csv.DictWriter(fout, fieldnames=out_cols)
    writer.writeheader()
    writer.writerows(enriched)

n = len(enriched)
print(f'\nWrote {n} rows → {OUTPUT}')

# ── QA ────────────────────────────────────────────────────────────────────────
with open(OUTPUT, newline='') as f:
    check = list(csv.DictReader(f))

for col in ['home_ops', 'home_p_era', 'away_ops', 'away_p_era']:
    filled = sum(1 for r in check if r.get(col))
    print(f'  {col:<14}: {filled}/{n} ({100*filled//n}%)')

print('\nSample (3 rows):')
for r in check[:3]:
    print(f'  {r["video_id"]}  h_ops={r["home_ops"]}  h_era={r["home_p_era"]}'
          f'  a_ops={r["away_ops"]}  a_era={r["away_p_era"]}'
          f'  h_hr/g={r["home_hr_g"]}  a_hr/g={r["away_hr_g"]}')
