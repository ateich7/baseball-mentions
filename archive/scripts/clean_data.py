import csv, re, shutil
from datetime import datetime

INPUT = 'game_labels.csv'
OUTPUT = 'game_labels_clean.csv'
shutil.copy(INPUT, INPUT + '.bak')

# ── Canonical team names ───────────────────────────────────────────────────
# Map every variant (nickname, typo, partial) → canonical full name
TEAM_MAP = {
    # AL East
    'yankees': 'new york yankees',
    'new york yankees': 'new york yankees',
    'red sox': 'boston red sox',
    'boston red sox': 'boston red sox',
    'blue jays': 'toronto blue jays',
    'blue jay': 'toronto blue jays',   # typo in one title
    'toronto blue jays': 'toronto blue jays',
    'orioles': 'baltimore orioles',
    'baltimore orioles': 'baltimore orioles',
    'rays': 'tampa bay rays',
    'tampa bay rays': 'tampa bay rays',
    # AL Central
    'white sox': 'chicago white sox',
    'chicago white sox': 'chicago white sox',
    'guardians': 'cleveland guardians',
    'cleveland guardians': 'cleveland guardians',
    'tigers': 'detroit tigers',
    'detroit tigers': 'detroit tigers',
    'royals': 'kansas city royals',
    'kansas city royals': 'kansas city royals',
    'twins': 'minnesota twins',
    'minnesota twins': 'minnesota twins',
    # AL West
    'astros': 'houston astros',
    'houston astros': 'houston astros',
    'athletics': 'athletics',
    'oakland athletics': 'athletics',
    'seattle mariners': 'seattle mariners',
    'mariners': 'seattle mariners',
    'angels': 'los angeles angels',
    'los angeles angels': 'los angeles angels',
    'rangers': 'texas rangers',
    'texas rangers': 'texas rangers',
    # NL East
    'braves': 'atlanta braves',
    'atlanta braves': 'atlanta braves',
    'marlins': 'miami marlins',
    'miami marlins': 'miami marlins',
    'mets': 'new york mets',
    'new york mets': 'new york mets',
    'phillies': 'philadelphia phillies',
    'philadelphia phillies': 'philadelphia phillies',
    'nationals': 'washington nationals',
    'washington nationals': 'washington nationals',
    # NL Central
    'cubs': 'chicago cubs',
    'chicago cubs': 'chicago cubs',
    'reds': 'cincinnati reds',
    'red': 'cincinnati reds',           # typo: "Red vs Giants"
    'cincinnati reds': 'cincinnati reds',
    'brewers': 'milwaukee brewers',
    'milwaukee brewers': 'milwaukee brewers',
    'pirates': 'pittsburgh pirates',
    'pittsburgh pirates': 'pittsburgh pirates',
    'cardinals': 'st. louis cardinals',
    'st louis cardinals': 'st. louis cardinals',
    'st. louis cardinals': 'st. louis cardinals',
    'st': 'st. louis cardinals',        # truncated parse
    # NL West
    'dodgers': 'los angeles dodgers',
    'los angeles dodgers': 'los angeles dodgers',
    'giants': 'san francisco giants',
    'san francisco giants': 'san francisco giants',
    'padres': 'san diego padres',
    'san diego padres': 'san diego padres',
    'rockies': 'colorado rockies',
    'colorado rockies': 'colorado rockies',
    'diamondbacks': 'arizona diamondbacks',
    'dbacks': 'arizona diamondbacks',
    'arizona diamondbacks': 'arizona diamondbacks',
}

# Sorted longest-first so we match "boston red sox" before "red sox" before "red"
TEAM_KEYS_BY_LEN = sorted(TEAM_MAP.keys(), key=len, reverse=True)


def normalize_team(raw: str) -> str:
    """Return canonical team name, or original string if unrecognized."""
    if not raw:
        return raw
    s = raw.strip().lower()
    # Direct lookup first
    if s in TEAM_MAP:
        return TEAM_MAP[s]
    # Strip title junk: find the longest known team name at the start of s
    for key in TEAM_KEYS_BY_LEN:
        if s.startswith(key):
            return TEAM_MAP[key]
    return raw.strip()


# ── Date normalization ─────────────────────────────────────────────────────
MONTH_MAP = {
    'january': 1, 'jan': 1,
    'february': 2, 'feb': 2,
    'march': 3, 'mar': 3,
    'april': 4, 'apr': 4,
    'may': 5,
    'june': 6, 'jun': 6,
    'july': 7, 'jul': 7,
    'august': 8, 'aug': 8,
    'september': 9, 'sep': 9,
    'october': 10, 'oct': 10,
    'november': 11, 'nov': 11,
    'december': 12, 'dec': 12,
}

def parse_date(s: str) -> str:
    """Parse any date string to YYYY-MM-DD, or '' if unparseable."""
    if not s:
        return ''
    s = s.strip().lower()

    # M/D/YY or M/D/YYYY
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', s)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        return f'{y:04d}-{mo:02d}-{d:02d}'

    # "april 19, 2026" or "april 19 2026"
    m = re.match(r'^([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$', s)
    if m:
        mo = MONTH_MAP.get(m.group(1))
        if mo:
            return f'{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}'

    # "july 7, 2025" already covered above; also handle no-comma variant
    m = re.match(r'^([a-z]+)\s+(\d{1,2})\s+(\d{4})$', s)
    if m:
        mo = MONTH_MAP.get(m.group(1))
        if mo:
            return f'{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}'

    return ''


def extract_date_from_title(title: str) -> str:
    """Try to pull a date out of a video title when date_raw is empty."""
    t = title.lower()

    # (MM-DD-YYYY) parenthesized format used by @baseballsavant
    m = re.search(r'\((\d{2})-(\d{2})-(\d{4})\)', t)
    if m:
        return f'{m.group(3)}-{m.group(1)}-{m.group(2)}'

    # MM-DD-YYYY bare
    m = re.search(r'(\d{2})-(\d{2})-(\d{4})', t)
    if m:
        return f'{m.group(3)}-{m.group(1)}-{m.group(2)}'

    # "month D, YYYY"
    m = re.search(
        r'(january|february|march|april|may|june|july|august|'
        r'september|october|november|december)\s+(\d{1,2}),?\s+(\d{4})', t)
    if m:
        mo = MONTH_MAP[m.group(1)]
        return f'{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}'

    # M/D/YY or M/D/YYYY embedded in title
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', t)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        return f'{y:04d}-{mo:02d}-{d:02d}'

    # "April 6 MLB" / "April 5 MLB" — month+day, no year.
    # These are from the 2026 season (Opening Day was March 27, 2026).
    m = re.search(
        r'(january|february|march|april|may|june|july|august|'
        r'september|october|november|december)\s+(\d{1,2})\s+mlb\b', t)
    if m:
        mo = MONTH_MAP[m.group(1)]
        d = int(m.group(2))
        return f'2026-{mo:02d}-{d:02d}'

    # "September X" with no year at end of title (DodgersGameHub channel)
    m = re.search(
        r'(september|october)\s+(\d{1,2})\s*$', t)
    if m:
        mo = MONTH_MAP[m.group(1)]
        d = int(m.group(2))
        return f'2025-{mo:02d}-{d:02d}'

    return ''


# ── Manual fixes for rows where the title regex misfired ─────────────────
MANUAL_FIXES = {
    # video_id: (team_1, team_2)
    # Bad regex parses
    'bI-jTDny344': ('seattle mariners', 'detroit tigers'),
    'j7vLUX4XyoQ': ('new york yankees', 'toronto blue jays'),
    '_jc2UrbEm8E': ('toronto blue jays', 'new york yankees'),
    '9HQ_mYBHO1s': ('miami marlins', 'new york yankees'),
    'hYSuaXeJOnM': ('cincinnati reds', 'san francisco giants'),
    # 2025 World Series: Dodgers vs Blue Jays (confirmed by SU_bMo561b8 title)
    'tc-kUUJEaDw': ('los angeles dodgers', 'toronto blue jays'),   # WS Game 6
    '96gSTHfYz6s': ('los angeles dodgers', 'toronto blue jays'),   # WS Game 5
    'l6-5sUsj9J4': ('toronto blue jays', 'los angeles dodgers'),   # WS Game 4 (Jays home)
    'UEv1WTdLpQc': ('los angeles dodgers', 'toronto blue jays'),   # WS Game 3
    '6BrK9Ep5M6o': ('toronto blue jays', 'los angeles dodgers'),   # WS Game 1 (Jays home)
    # 2025 ALCS: Blue Jays vs Mariners (Mariners won ALDS vs Tigers; Blue Jays won ALDS vs Yankees)
    'uGhcenWa9ls': ('toronto blue jays', 'seattle mariners'),      # ALCS Game 7
    'kAQ0jOEderM': ('seattle mariners', 'toronto blue jays'),      # ALCS Game 5
    # 2025 NLCS: Dodgers vs Brewers (ASMACLOTH semi-finals data)
    '45Rqrp4aZM4': ('los angeles dodgers', 'milwaukee brewers'),   # NLCS Game 4
    # 2025 NLDS: Dodgers vs Phillies (title: "Dodgers walk off Phillies to advance to NLCS")
    '3sxKL751dfM': ('los angeles dodgers', 'philadelphia phillies'),
    # Regular season games with no team parse
    '7OUXx8NZc48': ('los angeles dodgers', 'baltimore orioles'),   # Yamamoto (Dodgers pitcher)
    'bXO0-MgIJMM': ('los angeles dodgers', 'new york yankees'),    # "Dodgers defeat Yankees 18-2"
}

# Rows to drop entirely (not MLB games)
DROP_IDS = {
    '_6vFsHOc560',   # NCAA Football bowl game
    'VoPkfZk_lWQ',   # NCAA Football bowl game (Tennessee vs Clemson)
}

# ── Main cleaning pass ─────────────────────────────────────────────────────
with open(INPUT, newline='') as fin:
    reader = csv.DictReader(fin)
    fieldnames = reader.fieldnames
    rows = list(reader)

kept, dropped, fixed_teams, fixed_dates = [], [], 0, 0

for row in rows:
    vid = row['video_id']

    if vid in DROP_IDS:
        dropped.append(vid)
        continue

    # Manual team fixes
    if vid in MANUAL_FIXES:
        row['team_1'], row['team_2'] = MANUAL_FIXES[vid]
        fixed_teams += 1
    else:
        orig_t1, orig_t2 = row['team_1'], row['team_2']
        row['team_1'] = normalize_team(row['team_1'])
        row['team_2'] = normalize_team(row['team_2'])
        if row['team_1'] != orig_t1 or row['team_2'] != orig_t2:
            fixed_teams += 1

    # Date normalization
    orig_date = row['date_raw']
    if orig_date:
        row['date_raw'] = parse_date(orig_date)
    else:
        row['date_raw'] = extract_date_from_title(row['title'])
    if row['date_raw'] != orig_date:
        fixed_dates += 1

    # For rows with no teams parsed, try extracting from "(Team vs. Team)" in title
    if not row['team_1'] and not row['team_2']:
        m = re.search(r'\(([a-z\s]+)\s+vs\.?\s+([a-z\s]+)\)', row['title'].lower())
        if m:
            row['team_1'] = normalize_team(m.group(1).strip())
            row['team_2'] = normalize_team(m.group(2).strip())
            if row['team_1'] != m.group(1).strip() or row['team_2'] != m.group(2).strip():
                fixed_teams += 1

    kept.append(row)

with open(OUTPUT, 'w', newline='') as fout:
    writer = csv.DictWriter(fout, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(kept)

print(f'Input rows  : {len(rows)}')
print(f'Dropped     : {len(dropped)} — {dropped}')
print(f'Output rows : {len(kept)}')
print(f'Team fixes  : {fixed_teams}')
print(f'Date fixes  : {fixed_dates}')

# ── Quick QA ──────────────────────────────────────────────────────────────
with open(OUTPUT, newline='') as f:
    clean = list(csv.DictReader(f))

still_dirty_t2 = [
    r for r in clean
    if any(x in r['team_2'] for x in [
        'full game', 'replay', 'alds', 'alwc', 'doubleheader',
        'showdown', 'finish', 'battle', 'ending', 'moments',
        'matchup', 'clash', 'intense', 'wild', 'crazy', 'explosive',
        'insane', 'clutch', 'shocking', 'heated', 'epic', 'mlb full',
    ])
]
if still_dirty_t2:
    print(f'\nWARNING: {len(still_dirty_t2)} team_2 still look dirty:')
    for r in still_dirty_t2:
        print(f'  {r["video_id"]}: {r["team_2"]!r}')
else:
    print('\nteam_2: all clean')

no_date = [r for r in clean if not r['date_raw']]
print(f'Rows still missing date: {len(no_date)}')
if no_date:
    for r in no_date[:10]:
        print(f'  {r["video_id"]}: {r["title"][:70]}')
