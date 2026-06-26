"""
enrich_data.py — pass 2 of the training-data pipeline.

Hydrates game_labels_clean.csv with MLB Stats API data:
  • Schedule: venue, weather, team records, game type
  • Boxscore: starting pitchers, attendance (schedule never returns it)
  • Pitcher season stats: ERA, IP, K/9, BB/9

Three-pass design:
  1. One schedule + boxscore call per game (collect pitcher IDs)
  2. One stats call per unique (pitcher_id, season) pair
  3. Attach pitcher stats back to each row

Output: game_labels_enriched.csv
"""
import csv, re
from datetime import date

from utils import (
    get_json, ip_to_float, pitcher_stats_cols,
    fetch_pitcher_season_stats, parse_wind, TRADE_DEADLINES,
)

INPUT  = 'game_labels_clean.csv'
OUTPUT = 'game_labels_enriched.csv'

GAME_TYPE_DESC = {
    'R': 'Regular Season', 'S': 'Spring Training',
    'E': 'Exhibition',     'A': 'All-Star Game',
    'F': 'Wild Card',      'D': 'Division Series',
    'L': 'LCS',            'W': 'World Series',
}

NEW_COLS = [
    'venue_id', 'venue_name', 'game_type', 'game_type_desc',
    'series_game_num', 'day_night', 'attendance',
    'temp_f', 'sky', 'wind_speed_mph', 'wind_dir',
    'home_wins', 'home_losses', 'away_wins', 'away_losses',
    'home_sp_id', 'home_sp_name', 'home_sp_era', 'home_sp_ip',
    'home_sp_k9', 'home_sp_bb9',
    'away_sp_id', 'away_sp_name', 'away_sp_era', 'away_sp_ip',
    'away_sp_k9', 'away_sp_bb9',
    'is_playoff', 'days_to_trade_deadline',
]

# ── Load CSV ──────────────────────────────────────────────────────────────────
with open(INPUT, newline='') as f:
    reader    = csv.DictReader(f)
    orig_cols = reader.fieldnames
    rows      = list(reader)

print(f'Loaded {len(rows)} rows from {INPUT}')


# ── Pass 1: schedule + boxscore per game ──────────────────────────────────────
enrichment   = {}   # video_id → {col: value}
pitcher_ids  = {}   # (pitcher_id, season) → stats dict (None = not yet fetched)

for i, row in enumerate(rows):
    vid = row['video_id']
    pk  = row.get('game_pk', '').strip()
    out = {c: '' for c in NEW_COLS}

    if not pk:
        enrichment[vid] = out
        continue

    print(f'[{i+1}/{len(rows)}] {vid}  pk={pk}', end=' ', flush=True)

    # Date-derived fields
    try:
        gd       = date.fromisoformat(row.get('date_raw', ''))
        season   = gd.year
        deadline = TRADE_DEADLINES.get(season, date(season, 7, 31))
        out['days_to_trade_deadline'] = (deadline - gd).days
    except:
        season = 2025

    # ── Schedule: venue, weather, records, game type ──────────────────────────
    sched = get_json(
        f'https://statsapi.mlb.com/api/v1/schedule?sportId=1'
        f'&gamePks={pk}&hydrate=venue,weather,team'
    )
    if sched:
        for day in sched.get('dates', []):
            for g in day.get('games', []):
                out['game_type']      = g.get('gameType', '')
                out['game_type_desc'] = GAME_TYPE_DESC.get(out['game_type'], out['game_type'])
                out['is_playoff']     = 0 if out['game_type'] == 'R' else 1
                out['series_game_num'] = g.get('seriesGameNumber', '')
                out['day_night']      = g.get('dayNight', '')

                att = g.get('attendance')
                if att:
                    out['attendance'] = att

                v = g.get('venue', {})
                out['venue_id']   = v.get('id', '')
                out['venue_name'] = v.get('name', '')

                w = g.get('weather', {})
                out['temp_f'] = w.get('temp', '')
                out['sky']    = w.get('condition', '')
                spd, dir_ = parse_wind(w.get('wind', ''))
                if spd is not None:
                    out['wind_speed_mph'] = spd
                    out['wind_dir']       = dir_

                for side in ('home', 'away'):
                    rec = g.get('teams', {}).get(side, {}).get('leagueRecord', {})
                    out[f'{side}_wins']   = rec.get('wins', '')
                    out[f'{side}_losses'] = rec.get('losses', '')

    # ── Boxscore: actual starters, attendance/weather fallback ────────────────
    box = get_json(f'https://statsapi.mlb.com/api/v1/game/{pk}/boxscore')
    if box:
        for side in ('home', 'away'):
            key      = f'{side}_sp'
            pitchers = box.get('teams', {}).get(side, {}).get('pitchers', [])
            if not pitchers:
                continue
            sid     = pitchers[0]
            players = box.get('teams', {}).get(side, {}).get('players', {})
            p       = players.get(f'ID{sid}', {})
            out[f'{key}_id']   = sid
            out[f'{key}_name'] = p.get('person', {}).get('fullName', '')
            if (sid, season) not in pitcher_ids:
                pitcher_ids[(sid, season)] = None   # mark for fetch

        # Boxscore info[] for attendance + weather fallback
        info = {x.get('label', '').lower(): x.get('value', '')
                for x in box.get('info', []) if x.get('label')}
        # Attendance: schedule endpoint never returns it; always use info[]
        att_s = info.get('att', '')
        if att_s:
            out['attendance'] = re.sub(r'[^\d]', '', att_s)
        # Weather fallback if schedule hydration returned nothing
        if not out['temp_f']:
            wx = info.get('weather', '')
            if wx:
                m = re.search(r'(\d+)\s*degrees', wx, re.I)
                if m:
                    out['temp_f'] = int(m.group(1))
                m = re.search(r'degrees[,\s]+(.+?)\.?\s*$', wx, re.I)
                if m:
                    out['sky'] = m.group(1).strip()
        if not out['wind_speed_mph']:
            spd, dir_ = parse_wind(info.get('wind', ''))
            if spd is not None:
                out['wind_speed_mph'] = spd
                out['wind_dir']       = dir_

    enrichment[vid] = out
    print('✓', flush=True)
    time.sleep(0.15)


# ── Pass 2: pitcher season stats (one call per unique pitcher+season) ─────────
unique_pitchers = list(pitcher_ids.keys())
print(f'\nFetching regular-season stats for {len(unique_pitchers)} pitcher-seasons...')

for j, (pid, season) in enumerate(unique_pitchers):
    pitcher_ids[(pid, season)] = fetch_pitcher_season_stats(pid, season)
    if (j + 1) % 20 == 0:
        print(f'  {j+1}/{len(unique_pitchers)}')

print('Pitcher stats fetched.')


# ── Pass 3: attach pitcher stats to enrichment rows ───────────────────────────
for row in rows:
    vid = row['video_id']
    out = enrichment.get(vid, {c: '' for c in NEW_COLS})
    try:
        season = date.fromisoformat(row.get('date_raw', '')).year
    except:
        season = 2025

    for side in ('home', 'away'):
        key = f'{side}_sp'
        pid = out.get(f'{key}_id')
        if pid:
            stats = pitcher_ids.get((pid, season)) or {}
            era, ip, k9, bb9 = pitcher_stats_cols(stats)
            out[f'{key}_era'] = era
            out[f'{key}_ip']  = ip
            out[f'{key}_k9']  = k9
            out[f'{key}_bb9'] = bb9
    enrichment[vid] = out


# ── Write output ─────────────────────────────────────────────────────────────
out_cols = orig_cols + NEW_COLS
with open(OUTPUT, 'w', newline='') as fout:
    writer = csv.DictWriter(fout, fieldnames=out_cols)
    writer.writeheader()
    for row in rows:
        enriched = enrichment.get(row['video_id'], {c: '' for c in NEW_COLS})
        writer.writerow({**row, **enriched})

print(f'\nWrote {len(rows)} rows → {OUTPUT}')

# ── Quick QA ──────────────────────────────────────────────────────────────────
with open(OUTPUT, newline='') as f:
    clean = list(csv.DictReader(f))

missing_venue   = sum(1 for r in clean if not r['venue_name'])
missing_weather = sum(1 for r in clean if not r['temp_f'])
missing_sp      = sum(1 for r in clean if not r['home_sp_name'] or not r['away_sp_name'])
missing_era     = sum(1 for r in clean if not r['home_sp_era'] or not r['away_sp_era'])

print(f'\nQA:')
print(f'  Missing venue    : {missing_venue}')
print(f'  Missing weather  : {missing_weather}')
print(f'  Missing SP name  : {missing_sp}')
print(f'  Missing SP ERA   : {missing_era}')
