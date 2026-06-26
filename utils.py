"""
utils.py — shared helpers for the baseball-mentions pipeline.

Imported by: enrich_data, add_team_stats, add_broadcasters,
             kalshi_outcomes, bet_today, check_results.
"""

import json, re, time, urllib.request
from datetime import date

# ── Shared constants ──────────────────────────────────────────────────────────

# MLB trade deadlines (used to compute days_to_trade_deadline feature).
# The deadline tends to drive broadcast mentions of "trade" — games near the
# deadline have more trade-rumour discussion by announcers.
TRADE_DEADLINES = {
    2023: date(2023, 8, 1),
    2024: date(2024, 7, 30),
    2025: date(2025, 7, 31),
    2026: date(2026, 7, 31),
    2027: date(2027, 7, 31),
}

# Words with confirmed positive ROI — full Kelly sizing.
# Updated 2026-05-22 based on settled P&L review (wild pitch +29%, trade +19%,
# what a catch +17%, triple +10%).
ALLOWED_WORDS = {'wild pitch', 'triple', 'what a catch', 'trade'}

# Words under evaluation — bet exactly 1 contract to accumulate observations
# without risking meaningful capital.
# bullpen/pinch hit/perfect game: low data (44 games), gathering samples
# double play: best LR AUC (0.665) but only 7 NO outcomes in 76 games — validating signal
TEST_WORDS = {'bullpen', 'pinch hit', 'perfect game', 'double play'}

# Kalshi ticker suffix → display word (with spaces as they appear in model dict keys).
# The suffix is the last segment of a KXMLBMENTION market ticker, e.g.:
#   KXMLBMENTION-26MAY07NYYBOS-WALK → suffix 'WALK' → word 'walk off'
# NQE = "Event does not qualify" — Kalshi uses this when a game is postponed or
#        doesn't meet their criteria; None means skip this market entirely.
TICKER_SUFFIX_TO_WORD = {
    'BULL': 'bullpen',      'PINC': 'pinch hit',   'MVP':  'mvp',
    'PERF': 'perfect game', 'PITC': 'pitch clock',  'GRAN': 'grand slam',
    'EXTR': 'extra inning', 'TRAD': 'trade',        'BASE': 'bases loaded',
    'TRIP': 'triple',       'WHAT': 'what a catch', 'WALK': 'walk off',
    'BUNT': 'bunt',         'ERRO': 'error',        'WILD': 'wild pitch',
    'DOUB': 'double play',  'NQE':  None,
}

# ── HTTP ──────────────────────────────────────────────────────────────────────

_MLB_HEADERS = {
    'Accept': 'application/json',
    'User-Agent': 'Mozilla/5.0 (compatible; baseball-mentions/1.0)',
}

def get_json(url, retries=3):
    """GET a URL, return parsed JSON. Retries with exponential backoff on failure."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_MLB_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)   # 1s, 2s, 4s...
            else:
                print(f'  WARN: {url}: {e}')
    return None

# ── Parsing ───────────────────────────────────────────────────────────────────

def ip_to_float(ip_str) -> float:
    """Convert baseball innings-pitched notation to a float.

    Baseball records IP as whole.outs: '7.2' means 7 innings and 2 outs.
    This is NOT a decimal — '7.2' = 7 + 2/3 = 7.667 innings, not 7.2.
    The .1 and .2 represent thirds-of-an-inning (one out / two outs).
    """
    try:
        parts = str(ip_str).split('.')
        return int(parts[0]) + int(parts[1] if len(parts) > 1 else 0) / 3
    except Exception:
        return 0.0


def parse_wind(wind_str: str) -> tuple:
    """Parse '15 mph, In From LF' → (15, 'In From LF').
    The MLB API returns wind as a freeform string; we extract speed and direction.
    Returns (None, '') if wind_str is empty or unparseable."""
    if not wind_str:
        return None, ''
    speed, direction = None, ''
    m = re.search(r'(\d+)\s*mph', wind_str, re.I)
    if m:
        speed = int(m.group(1))
    # Direction is everything after 'mph,' — e.g. 'In From LF', 'Out to RF'
    m = re.search(r'mph[,\s]+(.+)', wind_str, re.I)
    if m:
        direction = m.group(1).strip()
    return speed, direction

# ── MLB Stats API helpers ─────────────────────────────────────────────────────

def pitcher_stats_cols(stats: dict) -> tuple:
    """Return (era, ip, k9, bb9) from a season-stats dict, or ('','','','').
    k9 and bb9 are computed from raw counts (strikeOuts / ip * 9) rather than
    the API's pre-computed strikeoutsPer9Inn, which can differ for partial seasons.
    """
    if not stats:
        return '', '', '', ''
    era = stats.get('era', '')
    ip  = ip_to_float(stats.get('inningsPitched', 0))
    k   = int(stats.get('strikeOuts', 0) or 0)
    bb  = int(stats.get('baseOnBalls', 0) or 0)
    k9  = round(k  / ip * 9, 2) if ip > 0 else ''
    bb9 = round(bb / ip * 9, 2) if ip > 0 else ''
    return era, (round(ip, 1) if ip else ''), k9, bb9


def fetch_pitcher_season_stats(pitcher_id, season: int) -> dict:
    """Fetch season pitching stats for one pitcher from the MLB Stats API.
    Returns the raw stat dict (keys: era, inningsPitched, strikeOuts, etc.)."""
    url = (f'https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats'
           f'?stats=season&group=pitching&season={season}&gameType=R')
    data = get_json(url)
    time.sleep(0.15)   # stay within MLB API rate limits
    if not data:
        return {}
    for sg in data.get('stats', []):
        splits = sg.get('splits', [])
        if splits:
            return splits[0].get('stat', {})
    return {}


def fetch_team_stats(team_id: int, season: int, end_date: str) -> tuple:
    """Return (ops, hr_g, r_g, era, whip, p_k9) for a team, season-to-date
    through end_date. Makes two API calls: one for hitting, one for pitching.

    end_date should be the day before the game so stats are 'as of game morning'.
    Returns empty strings for any stat the API doesn't have yet (e.g. opening day).
    """
    base = (f'https://statsapi.mlb.com/api/v1/teams/{team_id}/stats'
            f'?stats=byDateRange&season={season}'
            f'&startDate={season}-01-01&endDate={end_date}&gameType=R')

    # ── Hitting ───────────────────────────────────────────────────────────────
    ops = hr_g = r_g = ''
    hit = get_json(base + '&group=hitting')
    time.sleep(0.15)
    if hit:
        for sg in hit.get('stats', []):
            splits = sg.get('splits', [])
            if splits:
                s  = splits[0].get('stat', {})
                gp = int(s.get('gamesPlayed', 0) or 0)
                ops  = s.get('ops', '')
                hr   = int(s.get('homeRuns', 0) or 0)
                r    = int(s.get('runs', 0) or 0)
                hr_g = round(hr / gp, 3) if gp > 0 else ''
                r_g  = round(r  / gp, 2) if gp > 0 else ''
                break

    # ── Pitching ──────────────────────────────────────────────────────────────
    era = whip = p_k9 = ''
    pit = get_json(base + '&group=pitching')
    time.sleep(0.15)
    if pit:
        for sg in pit.get('stats', []):
            splits = sg.get('splits', [])
            if splits:
                s  = splits[0].get('stat', {})
                ip = ip_to_float(s.get('inningsPitched', 0))
                era  = s.get('era', '')
                whip = s.get('whip', '')
                k    = int(s.get('strikeOuts', 0) or 0)
                p_k9 = round(k / ip * 9, 2) if ip > 0 else ''
                break

    return ops, hr_g, r_g, era, whip, p_k9

# ── Broadcast / announcer helpers ─────────────────────────────────────────────

def extract_broadcasts(broadcasts: list) -> tuple:
    """Return (tv_national, tv_home) from a game's broadcast list.
    Filters to English TV only; deduplicates; skips out-of-market national fillers.
    tv_national can be pipe-separated if multiple national networks carry the game.
    """
    tv_en = [b for b in broadcasts
             if b.get('type') == 'TV' and b.get('language', 'en') == 'en']
    nat  = list(dict.fromkeys(
        b['name'] for b in tv_en
        if b.get('isNational') and 'out-of-market' not in b.get('name', '').lower()
    ))
    home = list(dict.fromkeys(
        b['name'] for b in tv_en
        if not b.get('isNational') and b.get('homeAway') == 'home'
    ))
    return ('|'.join(nat) if nat else ''), (home[0] if home else '')


def lookup_announcers(tv_home: str, tv_national: str, year: int) -> tuple:
    """Return (ann1, ann2) from the broadcast crew table.
    Prefers the home network over national because home crews call most games;
    national announcers only appear on nationally broadcast games.
    Returns ('', '') on table miss — transcript regex in add_broadcasters.py handles the fallback.
    """
    try:
        from broadcast_crews import CREW
        for network in (tv_home, tv_national):
            if not network:
                continue
            crew = CREW.get((network, year))
            if crew:
                return crew
    except ImportError:
        pass
    return '', ''
