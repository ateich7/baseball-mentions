Here's a README for the project:

---

# MLB Broadcast Word Prediction — Kalshi Betting Model

## Project Overview
This project builds a machine learning model to predict which words will be spoken during MLB game broadcasts, then bets on Kalshi prediction markets when our model's probability diverges meaningfully from the market's implied probability.

## How Kalshi MLB Word Markets Work
Kalshi offers pre-game contracts on whether specific words will be spoken during a broadcast. Each contract pays $1 if YES, $0 if NO. The price in cents equals the implied probability (e.g. a 35¢ contract implies 35% probability). YES + NO prices sum to slightly above 100¢ — the difference is Kalshi's vig. The true probability is the midpoint between YES-implied and NO-implied probabilities.

Target words tracked (one binary label per game):
`grand slam, triple, wild pitch, bullpen, double play, error, bases loaded, bunt, walk off, extra inning, perfect game, pitch clock, pinch hit, trade, mvp, what a catch`

## Betting Logic
- If our model says P(word spoken) = 0.70 but Kalshi's YES contract is priced at 0.45, that's +EV — buy YES
- If our model says P(word spoken) = 0.20 but Kalshi's NO contract implies 0.45, that's +EV — buy NO
- Only bet when the edge exceeds a threshold (e.g. >10 percentage points) to account for model uncertainty

## Data Pipeline

### Audio Collection
- YouTube full game broadcasts scraped from 8 channels using `yt-dlp`
- Channels: @NewYorkYankeesFullGames, @LetMePutYouOnGame80, @baseballsavant, @lynxsportshighlights500, @StephensBaseballArchives, @DodgersGameHub, @ASMACLOTH, @mlbfullgames99, plus two MLB official playlists
- Audio downloaded as MP3, transcribed with OpenAI Whisper (base model), then deleted
- Transcripts saved as plain `.txt` files, one per game

### Game Start/End Detection
Transcription covers the full video including pre/post-game. We detect the actual game window using:
- **Start**: first occurrence of broadcast phrases like "first pitch", "underway", "ball one", "strike one", etc.
- **End**: last occurrence of phrases like "final score", "ball game", "that will do it", etc.
- Word labels are only checked within the detected game window to avoid pre/post-game false positives

### Output: game_labels.csv
One row per game with columns:
- `video_id` — YouTube video ID
- `title` — video title
- `date_raw` — game date as parsed from title
- `team_1`, `team_2` — teams parsed from title
- `duration_minutes` — game length
- `channel` — source YouTube channel URL
- `transcript_file` — filename of corresponding transcript in `/transcripts/`
- One boolean column per target word (True/False)

## Model Architecture
- **Type**: XGBoost binary classifier, one model per target word
- **Input features** (planned): starting pitchers, lineups, ballpark, weather, broadcaster/channel, team offense/defense stats, day of season, playoff vs regular season
- **Target**: True/False — was the word spoken during the game?
- **Training**: ~600 labeled games across multiple teams, seasons (2023-2026), and broadcasters
- **Evaluation**: AUC-ROC per word, calibration against Kalshi market prices

## File Structure
```
/baseball/
├── game_labels.csv          # Main dataset — one row per game
├── pipeline.py              # Data collection pipeline
├── cookies.txt              # YouTube auth cookies (yt-dlp)
└── transcripts/
    └── [video_id].txt       # Full game transcript, one per game
```

## Current Status
~600 games labeled across Yankees, Dodgers, Angels, various other teams. Data collection complete. Next step: join MLB Stats API data, train XGBoost models, evaluate calibration, build Kalshi API integration for automated pre-game betting.

---

Save this as `README.md` in your `/workspace/baseball/` folder.