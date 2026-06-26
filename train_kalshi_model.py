"""
train_kalshi_model.py

Trains per-word logistic regression models using Kalshi outcome data as ground
truth.  With only 49 games, XGBoost would overfit; we use strong L2 logistic
regression with a small feature set selected by domain knowledge.

Output: models/kalshi_models.pkl
"""

import os, pickle, warnings
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import LeaveOneOut, cross_val_score
from sklearn.metrics import roc_auc_score, brier_score_loss
from sklearn.calibration import CalibratedClassifierCV
from sklearn.dummy import DummyClassifier

warnings.filterwarnings('ignore')
os.makedirs('models', exist_ok=True)

INPUT = 'kalshi_outcomes.csv'

# ── Target words ──────────────────────────────────────────────────────────────
TARGETS = [
    'grand_slam', 'triple', 'wild_pitch', 'bullpen', 'double_play', 'error',
    'bases_loaded', 'bunt', 'walk_off', 'extra_inning', 'perfect_game',
    'pitch_clock', 'pinch_hit', 'trade', 'mvp', 'what_a_catch',
]

# Column names used in train_models.py / bet_today.py (spaces, not underscores)
TARGETS_SPACED = [t.replace('_', ' ') for t in TARGETS]

# ── Feature subset — domain-selected for small-N regime ──────────────────────
# We deliberately limit to ~12 features to avoid overfitting 49 samples.
# Rationale per group:
#   Game context: series_game_num, days_to_trade_deadline, is_national, day_night
#   Weather/ballpark: temp_f, venue_id
#   Team quality: home_wins, away_wins (proxy for run environment)
#   Pitching: home_sp_era, away_sp_era (quality → wild_pitch, K, BB rates)
#   Offense: home_ops, away_ops (extra-base hits, bases loaded)

NUM_FEATURES = [
    'series_game_num', 'days_to_trade_deadline',
    'temp_f',
    'home_wins', 'away_wins',
    'home_sp_era', 'away_sp_era',
    'home_ops', 'away_ops',
]

# ── Load ──────────────────────────────────────────────────────────────────────
print(f'Loading {INPUT}...')
df = pd.read_csv(INPUT)
# Drop pandas-mangled duplicate columns (e.g. 'bunt.1', 'pinch_hit.2')
df = df.loc[:, ~df.columns.str.match(r'.+\.[0-9]+$')]
print(f'  {len(df)} rows, {len(df.columns)} columns')

# Drop rows where all targets are missing (shouldn't happen, but guard)
df = df.dropna(subset=[t for t in TARGETS if t in df.columns], how='all').reset_index(drop=True)

# ── Feature prep ──────────────────────────────────────────────────────────────
for c in NUM_FEATURES:
    df[c] = pd.to_numeric(df[c], errors='coerce')
    df[c] = df[c].fillna(df[c].median())

X_base = df[NUM_FEATURES].astype(float).values

scaler = StandardScaler()
X = scaler.fit_transform(X_base)
feature_names = NUM_FEATURES

print(f'  Feature matrix: {X.shape[0]} rows × {X.shape[1]} columns')

# ── Per-word training ─────────────────────────────────────────────────────────
print(f'\n{"Word":<18} {"N":>4} {"Base%":>6}  {"LOO AUC":>8} {"Brier":>7}  {"Note"}')
print('─' * 70)

models_out = {}

for word, word_spaced in zip(TARGETS, TARGETS_SPACED):
    if word not in df.columns:
        print(f'  {word:<16}  SKIP (not in CSV)')
        continue

    y_raw = pd.to_numeric(df[word], errors='coerce')
    valid  = ~y_raw.isna()
    y      = y_raw[valid].astype(int).values
    Xv     = X[valid.values]

    n         = len(y)
    base_rate = y.mean()

    if n < 10 or y.sum() < 2 or (y == 0).sum() < 2:
        # Not enough data — store base rate predictor only
        clipped = min(max(base_rate, 0.05), 0.95)
        models_out[word_spaced] = {
            'type':      'base_rate',
            'base_rate': clipped,
            'n':         n,
        }
        clip_note = f' (clipped from {base_rate:.1%})' if clipped != base_rate else ''
        print(f'  {word:<18} {n:>4} {clipped:>6.1%}  {"n/a":>8} {"n/a":>7}  base rate only{clip_note}')
        continue

    # Logistic regression with strong L2 regularisation
    # C=0.05 is deliberately conservative for the small-N regime
    lr = LogisticRegression(C=0.05, max_iter=2000, solver='lbfgs',
                            class_weight='balanced', random_state=42)

    # Leave-one-out CV for AUC (appropriate for n<60)
    loo = LeaveOneOut()
    try:
        loo_probs = np.zeros(n)
        for train_idx, test_idx in loo.split(Xv):
            lr.fit(Xv[train_idx], y[train_idx])
            loo_probs[test_idx] = lr.predict_proba(Xv[test_idx])[:, 1]

        auc   = roc_auc_score(y, loo_probs)
        brier = brier_score_loss(y, loo_probs)
        note  = 'good' if auc > 0.60 else 'weak'
    except Exception:
        auc = brier = float('nan')
        note = 'err'

    # Final model fit on all data
    lr.fit(Xv, y)

    # Use model only if LOO AUC > 0.58, otherwise fall back to base rate
    use_model = (not np.isnan(auc)) and auc > 0.58

    stored_base = min(max(base_rate, 0.05), 0.95)
    models_out[word_spaced] = {
        'type':      'lr' if use_model else 'base_rate',
        'model':     lr if use_model else None,
        'scaler':    scaler,
        'features':  feature_names,
        'base_rate': stored_base,
        'loo_auc':   auc,
        'brier':     brier,
        'n':         n,
    }

    print(f'  {word:<18} {n:>4} {base_rate:>6.1%}  {auc:>8.3f} {brier:>7.4f}  {note}{"*" if use_model else " (base)"}')

# ── Persist ───────────────────────────────────────────────────────────────────
bundle = {
    'models':        models_out,
    'feature_names': feature_names,
    'num_features':  NUM_FEATURES,
    'scaler':        scaler,
}

with open('models/kalshi_models.pkl', 'wb') as fh:
    pickle.dump(bundle, fh)
print(f'\n→ models/kalshi_models.pkl')

# ── Summary: hit rates vs typical Kalshi prices ───────────────────────────────
# Load open_* prices for comparison
print('\n── Base rates vs Kalshi opening prices ──────────────────────────────────')
print(f'{"Word":<18} {"Actual%":>8} {"Open¢ avg":>10} {"Open¢ med":>10} {"n_open":>6}')
print('─' * 60)

df2 = pd.read_csv(INPUT)
for word in TARGETS:
    if word not in df2.columns:
        continue
    y_raw = pd.to_numeric(df2[word], errors='coerce').dropna()
    if len(y_raw) == 0:
        continue
    base = y_raw.mean()

    open_col = f'open_{word}'
    if open_col in df2.columns:
        open_p = pd.to_numeric(df2[open_col], errors='coerce').dropna()
        n_open = len(open_p)
        avg_open = open_p.mean() if n_open > 0 else float('nan')
        med_open = open_p.median() if n_open > 0 else float('nan')
    else:
        avg_open = med_open = float('nan')
        n_open = 0

    note = ''
    if not np.isnan(avg_open):
        diff = base - avg_open
        if abs(diff) > 0.10:
            note = f'  ← {diff:+.0%}'

    print(f'  {word:<18} {base:>8.1%} {avg_open*100:>10.1f} {med_open*100:>10.1f} {n_open:>6}{note}')

print(f'\nNote: open_* prices are previous_yes_ask (last pre-settle price), not true')
print(f'      opening prices. They are BIASED upward for YES-resolving markets.')
print(f'      Use Kalshi live prices at bet time for edge calculation.')
