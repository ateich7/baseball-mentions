import csv

INPUT  = 'game_labels_final.csv'
OUTPUT = 'model_features.csv'

# Columns kept as row identifiers / metadata — not model inputs
METADATA = {
    'video_id', 'title', 'date_raw', 'channel', 'game_pk',
    'duration_minutes',      # post-game: unknown pre-game, correlated with extra_inning label
}

# Columns dropped entirely (pure redundancy or pipeline metadata)
DROP = {
    'transcript_file',       # always video_id + '.txt'
    'game_type_desc',        # text label of game_type
    'announcer_source',      # pipeline metadata
    'home_sp_name',          # human label for home_sp_id
    'away_sp_name',          # human label for away_sp_id
    'venue_name',            # human label for venue_id
    'team_1',                # raw title parse, superseded by home_team_id / away_team_id
    'team_2',                # same
    'is_playoff',            # derived: game_type != 'R'
}

TARGET_WORDS = [
    'grand slam', 'triple', 'wild pitch', 'bullpen', 'double play', 'error',
    'bases loaded', 'bunt', 'walk off', 'extra inning', 'perfect game',
    'pitch clock', 'pinch hit', 'trade', 'mvp', 'what a catch',
]

with open(INPUT, newline='') as f:
    reader    = csv.DictReader(f)
    orig_cols = reader.fieldnames
    rows      = list(reader)

kept_cols = [c for c in orig_cols if c not in DROP]

# Sanity-check: every dropped column actually exists
missing_drops = DROP - set(orig_cols)
if missing_drops:
    print(f'WARNING: columns to drop not found in input: {missing_drops}')

with open(OUTPUT, 'w', newline='') as fout:
    writer = csv.DictWriter(fout, fieldnames=kept_cols, extrasaction='ignore')
    writer.writeheader()
    writer.writerows(rows)

n = len(rows)
feature_cols  = [c for c in kept_cols if c not in METADATA and c not in TARGET_WORDS]
print(f'Input columns  : {len(orig_cols)}')
print(f'Dropped        : {len(DROP)}  ({", ".join(sorted(DROP))})')
print(f'Output columns : {len(kept_cols)}')
print(f'  Metadata     : {len([c for c in kept_cols if c in METADATA])}')
print(f'  Features     : {len(feature_cols)}')
print(f'  Targets      : {len([c for c in kept_cols if c in TARGET_WORDS])}')
print(f'Rows           : {n}')
print(f'\nFeature columns:')
for c in feature_cols:
    print(f'  {c}')
print(f'\nWrote {OUTPUT}')
