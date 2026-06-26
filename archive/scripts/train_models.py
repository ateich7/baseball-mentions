import os, pickle, warnings
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import OrdinalEncoder
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score, brier_score_loss
from sklearn.calibration import calibration_curve
import xgboost as xgb

warnings.filterwarnings('ignore')
os.makedirs('models', exist_ok=True)
os.makedirs('plots',  exist_ok=True)

# ── Config ────────────────────────────────────────────────────────────────────
TARGETS = [
    'grand slam', 'triple', 'wild pitch', 'bullpen', 'double play', 'error',
    'bases loaded', 'bunt', 'walk off', 'extra inning', 'perfect game',
    'pitch clock', 'pinch hit', 'trade', 'mvp', 'what a catch',
]

NUM_COLS = [
    'series_game_num', 'attendance', 'temp_f', 'wind_speed_mph',
    'home_wins', 'home_losses', 'away_wins', 'away_losses',
    'home_sp_era', 'home_sp_ip', 'home_sp_k9', 'home_sp_bb9',
    'away_sp_era', 'away_sp_ip', 'away_sp_k9', 'away_sp_bb9',
    'days_to_trade_deadline',
    'home_ops', 'home_hr_g', 'home_r_g', 'home_p_era', 'home_p_whip', 'home_p_k9',
    'away_ops', 'away_hr_g', 'away_r_g', 'away_p_era', 'away_p_whip', 'away_p_k9',
]
CAT_COLS = [
    'venue_id', 'game_type', 'day_night', 'sky', 'wind_dir',
    'tv_national', 'tv_home', 'announcer_1', 'announcer_2',
    'home_sp_id', 'away_sp_id', 'home_team_id', 'away_team_id',
]
ALL_FEATURES = NUM_COLS + CAT_COLS

XGB_PARAMS = dict(
    n_estimators=300, max_depth=4, learning_rate=0.05,
    subsample=0.8, colsample_bytree=0.7,
    min_child_weight=5, reg_alpha=0.1, reg_lambda=1.0,
    enable_categorical=True, eval_metric='logloss',
    verbosity=0, random_state=42,
)

# ── Load & clean ──────────────────────────────────────────────────────────────
print('Loading model_features.csv...')
df = pd.read_csv('model_features.csv')
print(f'  {len(df)} rows, {len(df.columns)} columns')

for t in TARGETS:
    df[t] = df[t].astype(int)

for c in NUM_COLS:
    df[c] = pd.to_numeric(df[c], errors='coerce')
    df[c] = df[c].fillna(df[c].median())

for c in CAT_COLS:
    df[c] = df[c].fillna('unknown').astype(str)
    df[c] = pd.Categorical(df[c])

# Save per-column category lists and numeric medians for prediction-time alignment
cat_categories = {c: list(df[c].cat.categories) for c in CAT_COLS}
num_medians    = {c: float(df[c].median()) for c in NUM_COLS}

df['date_raw'] = pd.to_datetime(df['date_raw'])
df = df.sort_values('date_raw').reset_index(drop=True)

# ── Time-based CV folds ───────────────────────────────────────────────────────
folds = []
for yr in [2024, 2025, 2026]:
    tr = df.index[df['date_raw'].dt.year < yr].tolist()
    te = df.index[df['date_raw'].dt.year == yr].tolist()
    if tr and te:
        folds.append((tr, te, yr))

print(f'\nTime-based CV folds:')
for tr, te, yr in folds:
    train_years = sorted(df.loc[tr, 'date_raw'].dt.year.unique().tolist())
    print(f'  train {train_years} ({len(tr)} rows)  →  test {yr} ({len(te)} rows)')

# ── Ordinal-encode for logistic regression ────────────────────────────────────
oe = OrdinalEncoder(handle_unknown='use_encoded_value', unknown_value=-1)
df_lr = df.copy()
df_lr[CAT_COLS] = oe.fit_transform(df_lr[CAT_COLS].astype(str))

# ── CV loop ───────────────────────────────────────────────────────────────────
print(f'\n{"Word":<18} {"Base%":>6}  {"LR AUC":>7} {"LR Brier":>9}  '
      f'{"XGB AUC":>8} {"XGB Brier":>9} {"Cal Brier":>9}')
print('─' * 80)

results   = {}
oof_preds = {t: np.full(len(df), np.nan) for t in TARGETS}

for word in TARGETS:
    y = df[word].values
    base_rate = y.mean()

    lr_aucs, lr_briers, xgb_aucs, xgb_briers = [], [], [], []

    for tr, te, yr in folds:
        y_tr, y_te = y[tr], y[te]
        if len(y_tr) < 30 or y_tr.sum() < 3 or (y_te == 1).sum() < 1 or (y_te == 0).sum() < 1:
            continue

        # Logistic Regression
        lr = LogisticRegression(max_iter=1000, class_weight='balanced', C=0.1)
        lr.fit(df_lr.loc[tr, ALL_FEATURES].values, y_tr)
        p_lr = lr.predict_proba(df_lr.loc[te, ALL_FEATURES].values)[:, 1]
        lr_aucs.append(roc_auc_score(y_te, p_lr))
        lr_briers.append(brier_score_loss(y_te, p_lr))

        # XGBoost
        pos_w = (y_tr == 0).sum() / max((y_tr == 1).sum(), 1)
        clf = xgb.XGBClassifier(**XGB_PARAMS, scale_pos_weight=pos_w)
        clf.fit(df.loc[tr, ALL_FEATURES], y_tr, verbose=False)
        p_xgb = clf.predict_proba(df.loc[te, ALL_FEATURES])[:, 1]
        oof_preds[word][te] = p_xgb
        xgb_aucs.append(roc_auc_score(y_te, p_xgb))
        xgb_briers.append(brier_score_loss(y_te, p_xgb))

    # Post-hoc calibration on all OOF predictions
    valid = ~np.isnan(oof_preds[word])
    cal_brier = np.nan
    if valid.sum() > 20 and len(np.unique(y[valid])) > 1:
        iso = IsotonicRegression(out_of_bounds='clip')
        iso.fit(oof_preds[word][valid], y[valid])
        cal_brier = brier_score_loss(y[valid], iso.predict(oof_preds[word][valid]))

    r = dict(
        base_rate=base_rate,
        lr_auc=np.mean(lr_aucs)   if lr_aucs   else np.nan,
        lr_brier=np.mean(lr_briers) if lr_briers else np.nan,
        xgb_auc=np.mean(xgb_aucs)  if xgb_aucs  else np.nan,
        xgb_brier=np.mean(xgb_briers) if xgb_briers else np.nan,
        cal_brier=cal_brier,
    )
    results[word] = r

    def _f(v): return f'{v:8.3f}' if not np.isnan(v) else '     n/a'
    print(f'  {word:<16} {base_rate:>6.1%}  {_f(r["lr_auc"])} {_f(r["lr_brier"])}  '
          f'{_f(r["xgb_auc"])} {_f(r["xgb_brier"])} {_f(r["cal_brier"])}')

# ── Calibration plots ─────────────────────────────────────────────────────────
print('\nGenerating calibration plots...')
fig, axes = plt.subplots(4, 4, figsize=(16, 14))
for ax, word in zip(axes.flatten(), TARGETS):
    p  = oof_preds[word]
    y  = df[word].values
    ok = ~np.isnan(p)
    if ok.sum() > 30 and len(np.unique(y[ok])) > 1:
        try:
            fp, mp = calibration_curve(y[ok], p[ok], n_bins=8, strategy='quantile')
            ax.plot(mp, fp, 's-', lw=1.5, label='XGBoost (raw)')
            iso = IsotonicRegression(out_of_bounds='clip')
            iso.fit(p[ok], y[ok])
            fp2, mp2 = calibration_curve(y[ok], iso.predict(p[ok]), n_bins=8, strategy='quantile')
            ax.plot(mp2, fp2, 'D--', lw=1.5, label='Calibrated')
        except Exception:
            pass
    ax.plot([0, 1], [0, 1], 'k:', alpha=0.4)
    ax.set_title(f'{word}\n(base {y.mean():.0%})', fontsize=9)
    ax.set_xlabel('Predicted', fontsize=8)
    ax.set_ylabel('Actual', fontsize=8)
    ax.legend(fontsize=7)

plt.suptitle('Calibration curves — OOF predictions (time-based CV)', fontsize=13, y=1.01)
plt.tight_layout()
plt.savefig('plots/calibration_curves.png', dpi=130, bbox_inches='tight')
print('  → plots/calibration_curves.png')

# ── Feature importance ────────────────────────────────────────────────────────
print('\nTraining final models on all data...')
final_models   = {}
importance_sum = {f: 0.0 for f in ALL_FEATURES}

for word in TARGETS:
    y     = df[word].values
    pos_w = (y == 0).sum() / max((y == 1).sum(), 1)
    clf   = xgb.XGBClassifier(**XGB_PARAMS, scale_pos_weight=pos_w)
    clf.fit(df[ALL_FEATURES], y, verbose=False)

    # Calibrator fitted on OOF predictions
    calibrator = None
    p_oof = oof_preds[word]
    valid = ~np.isnan(p_oof)
    if valid.sum() > 20 and len(np.unique(y[valid])) > 1:
        calibrator = IsotonicRegression(out_of_bounds='clip')
        calibrator.fit(p_oof[valid], y[valid])

    final_models[word] = {'xgb': clf, 'calibrator': calibrator,
                          'base_rate': y.mean()}
    for f, imp in zip(ALL_FEATURES, clf.feature_importances_):
        importance_sum[f] += imp
    print(f'  {word}', flush=True)

with open('models/final_models.pkl', 'wb') as fh:
    pickle.dump({'models': final_models, 'ordinal_encoder': oe,
                 'num_cols': NUM_COLS, 'cat_cols': CAT_COLS,
                 'cat_categories': cat_categories,
                 'num_medians': num_medians}, fh)
print('  → models/final_models.pkl')

# Feature importance bar chart
top = sorted(importance_sum.items(), key=lambda x: -x[1])[:20]
fig2, ax2 = plt.subplots(figsize=(9, 7))
names, vals = zip(*top)
ax2.barh(names[::-1], vals[::-1])
ax2.set_xlabel('Cumulative XGBoost importance (all 16 words)')
ax2.set_title('Top 20 features')
plt.tight_layout()
plt.savefig('plots/feature_importance.png', dpi=130, bbox_inches='tight')
print('  → plots/feature_importance.png')

# ── Summary ───────────────────────────────────────────────────────────────────
print('\n── Summary ──────────────────────────────────────────────────────────')
sorted_by_xgb = sorted(results.items(), key=lambda x: -x[1]['xgb_auc']
                        if not np.isnan(x[1]['xgb_auc']) else 0)
print(f'  Best predicted words (XGB AUC):')
for w, r in sorted_by_xgb[:5]:
    print(f'    {w:<18} AUC={r["xgb_auc"]:.3f}  Brier={r["xgb_brier"]:.4f}→{r["cal_brier"]:.4f}')
print(f'  Hardest words to predict:')
for w, r in sorted_by_xgb[-5:]:
    print(f'    {w:<18} AUC={r["xgb_auc"]:.3f}  Brier={r["xgb_brier"]:.4f}→{r["cal_brier"]:.4f}')

print(f'\nTop 10 features across all models:')
for feat, score in top[:10]:
    print(f'  {feat:<30} {score:.3f}')
