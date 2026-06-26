"""
add_broadcasters.py — pass 3 of the training-data pipeline.

Adds tv_national, tv_home, announcer_1, announcer_2 to each row.

Two-step lookup:
  1. Crew table (broadcast_crews.CREW): (network, year) → (ann1, ann2). Fast
     and accurate for all games in the table.
  2. Transcript regex fallback: scan the first 2500 chars of the transcript
     for self-introduction patterns like "I'm Dave O'Brien alongside...".

Input:  game_labels_enriched.csv
Output: game_labels_final.csv
"""
import csv, os, re, time
from pathlib import Path

from broadcast_crews import CREW
from utils import get_json, extract_broadcasts

INPUT       = 'game_labels_enriched.csv'
OUTPUT      = 'game_labels_final.csv'
TRANSCRIPTS = 'transcripts'
BATCH_SIZE  = 50

NEW_COLS = ['tv_national', 'tv_home', 'announcer_1', 'announcer_2', 'announcer_source']

# ── Transcript-based announcer extraction ─────────────────────────────────────
# Only used when the crew table has no entry. Patterns are ordered by confidence.

_N = r"[A-Z][a-zA-Z']+(?:\s[A-Z][a-zA-Z']+)+"

PATTERNS = [
    re.compile(rf'({_N})\s+alongside\s+(?:\w+\s+){{0,6}}({_N})'),
    re.compile(rf'({_N})\s+along\s+with\s+(?:\w+\s+){{0,4}}({_N})'),
    re.compile(rf'({_N})\s+here\s+(?:with|alongside)\s+(?:\w+\s+){{0,4}}({_N})'),
    re.compile(rf"[Ii]'?m\s+({_N})(?:\s+(?:alongside|with)\s+(?:\w+\s+){{0,4}}({_N}))?"),
    re.compile(rf'({_N})\s+and\s+({_N})\s+(?:on|have)\s+the\s+call'),
    re.compile(rf'({_N})\s+and\s+({_N})\s+(?:back\s+)?with\s+you'),
    re.compile(rf'[Hh]i[, ]+(?:everybody|everyone|folks)[.,!]?\s+({_N})'),
    re.compile(rf'joined\s+by\s+(?:\w+\s+){{0,4}}({_N})'),
]

# Phrases that match the name pattern but are not names
_EXCLUDE = {
    'American League', 'National League', 'Major League', 'Major League Baseball',
    'World Series', 'Spring Training', 'Opening Day', 'All Star',
    'New York', 'Los Angeles', 'San Francisco', 'San Diego', 'Kansas City',
    'Tampa Bay', 'Red Sox', 'Blue Jays', 'White Sox', 'Hall Fame',
    'Happy Easter', 'Baseball Hall', 'Big League', 'Minor League',
}

_CONTRACTION_RE = re.compile(r"(?i)^(i'?m|i've|we're|you're|it's|he's|she's|that's|who's)\b")

def _valid(name):
    if not name or len(name) < 5:
        return False
    if _CONTRACTION_RE.match(name):
        return False
    return not any(excl.lower() in name.lower() for excl in _EXCLUDE)


def extract_announcers(transcript_path):
    """Return (ann1, ann2) by scanning the first 2500 chars of a transcript."""
    try:
        text = Path(transcript_path).read_text(encoding='utf-8', errors='ignore')
    except Exception:
        return '', ''
    sample = text[:2500]
    for pat in PATTERNS:
        m = pat.search(sample)
        if not m:
            continue
        valid = [g.strip() for g in m.groups() if g and _valid(g.strip())]
        if valid:
            return valid[0], (valid[1] if len(valid) > 1 else '')
    return '', ''


# ── Load CSV ──────────────────────────────────────────────────────────────────
with open(INPUT, newline='') as f:
    reader    = csv.DictReader(f)
    orig_cols = reader.fieldnames
    rows      = list(reader)

print(f'Loaded {len(rows)} rows from {INPUT}')

# ── Pass 1: batch-fetch broadcast data ───────────────────────────────────────
unique_pks = list(dict.fromkeys(r['game_pk'] for r in rows if r.get('game_pk')))
print(f'Fetching broadcasts for {len(unique_pks)} games ({BATCH_SIZE}/batch)...')

broadcasts_by_pk = {}
for i in range(0, len(unique_pks), BATCH_SIZE):
    batch = unique_pks[i:i + BATCH_SIZE]
    url   = (f'https://statsapi.mlb.com/api/v1/schedule?sportId=1'
             f'&gamePks={",".join(batch)}&hydrate=broadcasts')
    data  = get_json(url)
    if data:
        for day in data.get('dates', []):
            for g in day.get('games', []):
                pk = str(g.get('gamePk', ''))
                broadcasts_by_pk[pk] = g.get('broadcasts', [])
    print(f'  {min(i+BATCH_SIZE, len(unique_pks))}/{len(unique_pks)}', flush=True)
    time.sleep(0.2)

print(f'Broadcast data received for {len(broadcasts_by_pk)} games')

# ── Pass 2: announcers (crew table → transcript fallback) ─────────────────────
print('\nLooking up announcers...')

out_cols = orig_cols + NEW_COLS
enriched = []
counts        = dict(tv_national=0, tv_home=0, announcer_1=0, announcer_2=0)
source_counts = dict(lookup=0, transcript=0, none=0)

for row in rows:
    pk            = row.get('game_pk', '')
    broadcasts    = broadcasts_by_pk.get(pk, [])
    tv_nat, tv_hm = extract_broadcasts(broadcasts)

    try:
        year = int(row.get('date_raw', '')[:4])
    except ValueError:
        year = 0

    # Prefer home network; fall back to national
    ann1, ann2, source = '', '', 'none'
    for network in (tv_hm, tv_nat):
        if not network:
            continue
        crew = CREW.get((network, year))
        if crew:
            ann1, ann2 = crew
            source = 'lookup'
            break

    # Transcript regex if crew table missed
    if not ann1:
        tf = row.get('transcript_file', '')
        if tf:
            ann1, ann2 = extract_announcers(os.path.join(TRANSCRIPTS, tf))
            if ann1:
                source = 'transcript'

    row.update({'tv_national': tv_nat, 'tv_home': tv_hm,
                'announcer_1': ann1, 'announcer_2': ann2,
                'announcer_source': source})
    enriched.append(row)

    if tv_nat: counts['tv_national'] += 1
    if tv_hm:  counts['tv_home']     += 1
    if ann1:   counts['announcer_1'] += 1
    if ann2:   counts['announcer_2'] += 1
    source_counts[source] += 1

# ── Write output ──────────────────────────────────────────────────────────────
with open(OUTPUT, 'w', newline='') as fout:
    writer = csv.DictWriter(fout, fieldnames=out_cols)
    writer.writeheader()
    writer.writerows(enriched)

n = len(enriched)
print(f'\nWrote {n} rows → {OUTPUT}')
print(f'\nFill rates:')
for col, cnt in counts.items():
    print(f'  {col:<16}: {cnt}/{n} ({100*cnt//n}%)')
print(f'\nAnnouncer source:')
for src, cnt in source_counts.items():
    print(f'  {src:<12}: {cnt}/{n} ({100*cnt//n}%)')

print('\nSample (first 10 rows with any broadcast/announcer data):')
found = 0
for r in enriched:
    if any(r.get(c) for c in NEW_COLS):
        print(f'  {r["video_id"]:15s}  nat={r["tv_national"]!r:20s}  home={r["tv_home"]!r:25s}'
              f'  ann1={r["announcer_1"]!r:20s}  ann2={r["announcer_2"]!r}')
        found += 1
        if found >= 10:
            break
