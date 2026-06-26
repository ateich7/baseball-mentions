import yt_dlp, os, re, csv, whisper, time

# ── Configuration ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, 'transcripts')
CSV_PATH = os.path.join(BASE_DIR, 'game_labels.csv')
COOKIES_PATH = os.path.join(BASE_DIR, 'cookies.txt')
os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)

channels = [
    'https://www.youtube.com/@NewYorkYankeesFullGames',
    'https://www.youtube.com/@LetMePutYouOnGame80',
    'https://www.youtube.com/@baseballsavant',
    'https://www.youtube.com/@lynxsportshighlights500',
    'https://www.youtube.com/@StephensBaseballArchives',
    'https://www.youtube.com/@DodgersGameHub',
    'https://www.youtube.com/@ASMACLOTH',
    'https://www.youtube.com/@mlbfullgames99',
    'https://www.youtube.com/playlist?list=PLL-lmlkrmJam3TSqyl_1HZ1k1QFNyLAK3',
    'https://www.youtube.com/playlist?list=PLL-lmlkrmJam1gL_8GKJAl5w3uK9L8O0P',
]

CUSTOM_TITLE_CHANNELS = {
    'https://www.youtube.com/@StephensBaseballArchives': {
        'min_hours': 2,
    }
}

GAMES_PER_CHANNEL = None  # None = no cap, process all

target_words = [
    'grand slam', 'triple', 'wild pitch', 'bullpen',
    'double play', 'error', 'bases loaded', 'bunt',
    'walk off', 'extra inning', 'perfect game', 'pitch clock',
    'pinch hit', 'trade', 'mvp', 'what a catch',
]

start_phrases = [
    'underway', 'under way', 'first pitch', 'play ball',
    'game underway', 'game is underway', 'we are underway',
    'and we are off', 'we are off and running',
    'swing and a miss', 'swing and a',
    'called strike one', 'strike one',
    'ball one', 'ball two',
    'high and outside', 'low and away',
    'inside corner', 'outside corner',
    'check swing', 'fouled off',
    'foul ball', 'foul tip',
    'in the dirt', 'high fastball',
    'breaking ball', 'off speed',
    'changeup', 'curveball',
    'slider', 'fastball',
    'the windup', 'the stretch',
    'the delivery', 'here is the pitch',
    'the pitch', 'delivers',
    'into the glove', 'popped him up',
    'ground ball', 'line drive',
    'fly ball', 'deep fly',
    'swung on and missed',
    'got him swinging',
    'looking for strike',
    'full count', 'two and two',
    'one and one', 'three and two',
    'ahead in the count', 'behind in the count',
    'throws to first', 'throws to second',
    'steps off the rubber',
    'runner takes his lead',
    'holding the runner',
    'batting average', 'on the season',
    'era this year', 'strikeouts on the year',
    'walks to the plate', 'steps in',
    'digs in', 'sets up',
    'looking in for the sign',
    'gets the sign', 'shakes off',
    'runners on base', 'nobody on',
    'man on first', 'man on second',
    'bases empty',
]

end_phrases = [
    'ball game', 'and that will do it', 'final score',
    'game over', 'that will do it', 'wins it',
    'that does it', 'ballgame is over',
    'and that is your ballgame', 'see you tomorrow',
    'final tonight', 'that is the ballgame',
]

TITLE_PHRASES = [
    'full game', 'full game replay', 'regular season full game',
    'complete game', 'game replay',
]

MLB_TEAMS = [
    'yankees', 'red sox', 'blue jays', 'orioles', 'rays',
    'white sox', 'guardians', 'tigers', 'royals', 'twins',
    'astros', 'athletics', 'mariners', 'angels', 'rangers',
    'braves', 'marlins', 'mets', 'phillies', 'nationals',
    'cubs', 'reds', 'brewers', 'pirates', 'cardinals',
    'dodgers', 'giants', 'padres', 'rockies', 'diamondbacks',
]

fieldnames = ['video_id', 'title', 'date_raw', 'team_1', 'team_2',
              'duration_minutes', 'channel', 'transcript_file'] + target_words

# ── Helpers ────────────────────────────────────────────────────
def parse_game_info(title):
    date = None
    for pattern in [
        r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4}',
        r'\d{1,2}/\d{1,2}/\d{2,4}',
        r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\.?\s+\d{1,2},?\s+\d{4}',
    ]:
        match = re.search(pattern, title.lower())
        if match:
            date = match.group(0)
            break
    teams = None
    team_match = re.search(r'([a-z\s]+)\s+(vs\.?|@|at)\s+([a-z\s]+)', title.lower())
    if team_match:
        teams = (team_match.group(1).strip(), team_match.group(3).strip())
    return date, teams


def is_mlb_game(title):
    return sum(1 for team in MLB_TEAMS if team in title.lower()) >= 2


def scrape_channel(channel_url):
    ydl_opts = {
        'quiet': True,
        'extract_flat': 'in_playlist',
        'playlist_items': '1:200',
        'ignore_errors': True,
    }
    videos = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if not info:
                return videos
            for entry in info.get('entries', []):
                if not entry:
                    continue
                if entry.get('_type') == 'playlist':
                    sub_url = entry.get('url') or entry.get('webpage_url')
                    if sub_url:
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl2:
                                sub_info = ydl2.extract_info(sub_url, download=False)
                                if sub_info:
                                    for sub_entry in sub_info.get('entries', []):
                                        if sub_entry and sub_entry.get('_type') != 'playlist':
                                            videos.append(sub_entry)
                        except Exception:
                            continue
                else:
                    videos.append(entry)
    except Exception as e:
        print(f'Error scraping {channel_url}: {e}')
    return videos


def get_full_game_urls(channel_url):
    results = []
    videos = scrape_channel(channel_url)
    custom = CUSTOM_TITLE_CHANNELS.get(channel_url)
    for video in videos:
        title = video.get('title', '') or ''
        duration = video.get('duration', 0) or 0
        video_id = video.get('id', '')
        if not video_id:
            continue
        title_lower = title.lower()
        if custom:
            passes = (
                duration / 3600 >= custom['min_hours'] and
                is_mlb_game(title) and
                'highlights' not in title_lower and
                'minor' not in title_lower
            )
        else:
            passes = (
                duration / 3600 >= 2 and
                any(phrase in title_lower for phrase in TITLE_PHRASES) and
                'highlights' not in title_lower
            )
        if passes:
            date, teams = parse_game_info(title)
            results.append({
                'video_id': video_id,
                'url': f'https://www.youtube.com/watch?v={video_id}',
                'title': title,
                'duration_minutes': round(duration / 60),
                'channel': channel_url,
                'date_raw': date,
                'team_1': teams[0] if teams else None,
                'team_2': teams[1] if teams else None,
            })
            print(f'Found: {title} ({round(duration/60)} min)')
    return results


def download_audio(url, video_id):
    time.sleep(10)
    audio_path = f'/tmp/{video_id}.mp3'
    ydl_opts = {
        'format': 'bestaudio',
        'outtmpl': f'/tmp/{video_id}.%(ext)s',
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
        'quiet': True,
        'cookiefile': COOKIES_PATH,
        'js_runtimes': {'node': {'path': '/opt/nvm/versions/node/v24.14.1/bin/node'}},
        'remote_components': ['ejs:github'],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return audio_path


def save_transcript(video_id, result):
    transcript_path = os.path.join(TRANSCRIPTS_DIR, f'{video_id}.txt')
    with open(transcript_path, 'w') as f:
        f.write(result['text'])
    return transcript_path


def detect_word_labels(result):
    first_pitch_time = None
    for segment in result['segments']:
        if any(phrase in segment['text'].lower() for phrase in start_phrases):
            first_pitch_time = segment['start']
            print(f'Game start detected at {first_pitch_time:.0f}s')
            break
    if first_pitch_time is None:
        print('WARNING: defaulting game start to 10 minutes')
        first_pitch_time = 600

    game_end_time = None
    for segment in reversed(result['segments']):
        if any(phrase in segment['text'].lower() for phrase in end_phrases):
            game_end_time = segment['end']
            print(f'Game end detected at {game_end_time:.0f}s')
            break
    if game_end_time is None:
        print('WARNING: defaulting game end to 5 minutes before end')
        game_end_time = result['segments'][-1]['end'] - 300

    word_found = {word: False for word in target_words}
    for segment in result['segments']:
        if segment['start'] < first_pitch_time:
            continue
        if segment['start'] > game_end_time:
            break
        text = segment['text'].lower()
        for word in target_words:
            if word in text:
                word_found[word] = True
    return word_found


def process_game(game, model):
    video_id = game['video_id']
    print(f'\nProcessing: {game["title"]}')
    audio_path = download_audio(game['url'], video_id)
    result = model.transcribe(audio_path, verbose=False)
    os.remove(audio_path)
    print('Audio deleted')
    save_transcript(video_id, result)
    print(f'Transcript saved: {video_id}.txt')
    word_labels = detect_word_labels(result)
    return {
        'video_id': video_id,
        'title': game['title'],
        'date_raw': game['date_raw'],
        'team_1': game['team_1'],
        'team_2': game['team_2'],
        'duration_minutes': game['duration_minutes'],
        'channel': game['channel'],
        'transcript_file': f'{video_id}.txt',
        **word_labels,
    }


def load_processed_ids():
    if not os.path.exists(CSV_PATH):
        return set()
    with open(CSV_PATH, 'r') as f:
        return {row['video_id'] for row in csv.DictReader(f)}


# ── Main ───────────────────────────────────────────────────────
if __name__ == '__main__':
    processed_ids = load_processed_ids()
    print(f'Already processed: {len(processed_ids)} games')

    all_games = []
    for channel in channels:
        print(f'\nScraping {channel}...')
        try:
            games = get_full_game_urls(channel)
            new_games = [g for g in games if g['video_id'] not in processed_ids]
            if GAMES_PER_CHANNEL is not None:
                new_games = new_games[:GAMES_PER_CHANNEL]
            all_games.extend(new_games)
            print(f'Queued {len(new_games)} games from this channel')
        except Exception as e:
            print(f'ERROR on {channel}: {e}')
            continue

    print(f'\nTotal games to process: {len(all_games)}')

    model = whisper.load_model('base')

    write_header = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for game in all_games:
            try:
                row = process_game(game, model)
                writer.writerow(row)
                f.flush()
                print(f'Saved: {game["video_id"]}')
            except Exception as e:
                print(f'Failed on {game["video_id"]}: {e}')
                continue

    print('\nDone.')