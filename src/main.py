import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import HTTPServer  # For WebDAVHandler
from itertools import groupby
import json
import locale
import os
from pathlib import Path
import random
import re
import requests
import subprocess
import threading
import time

import bcrypt
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Form, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import ffmpeg
import monitor
import pycountry
from pydantic import ValidationError
import rapidfuzz

import accounts
from auth import (
    TOKEN_COOKIE_NAME,
    COOKIE_MAX_AGE,
    PUBLIC_PATHS,
    PUBLIC_PATH_PREFIXES,
    is_valid_token,
)
import config
from log import log
from scrubber_generator import generate_sprite_sheet
from titles_data import parse_title_data
import tmdb
import transcode

app = FastAPI(title='Libertalia', description='A self-hosted media server')

# Separate from TOKEN_COOKIE_NAME (auth.py) — that one gates the whole site,
# this one identifies which account is logged in once past that gate.
SESSION_COOKIE_NAME = 'session_id'

ISO_639_2_NAMES = {
    lang.alpha_3: lang.name
    for lang in pycountry.languages
    if hasattr(lang, 'alpha_2') and hasattr(lang, 'alpha_3')
}

CONFIG = config.load_config()
# Populated at startup from config.jsonc's media_dirs
MEDIA_ROOTS: list[Path] = [Path('./media')]
WWW_ROOT = Path('./www')
CACHE_ROOT = Path('./cache')

cfg_require_token = False  # Default to False, overridden by config.jsonc if present

# Load the .env
load_dotenv(os.path.join('.', '.env'))
TMDB_TOKEN = os.getenv('TMDB_READ_ACCESS_TOKEN')

# In-memory cache for all title data, keyed by slug. Populated at startup by host_app().
_title_cache: dict[str, dict] = {}
_title_cache_lock = threading.Lock()

# In-memory cache for ffprobe'd file durations, keyed by (str(path), mtime)
_duration_cache: dict[tuple[str, float], int] = {}
_duration_cache_lock = threading.Lock()

def get_cached_title(slug: str) -> dict | None:
    # Returns the cached title data for `slug`, or None if not found. Thread-safe.
    with _title_cache_lock:
        return _title_cache.get(slug)

def get_cached_duration(path: Path) -> int | None:
    # Returns the cached duration in seconds for `path`, or None if not found. Thread-safe.
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
 
    cache_key = (str(path), mtime)
 
    with _duration_cache_lock:
        cached = _duration_cache.get(cache_key)
    if cached is not None:
        return cached
 
    try:
        probe = ffmpeg.probe(str(path))
        duration = int(float(probe['format']['duration']))
    except Exception:
        return None
 
    with _duration_cache_lock:
        _duration_cache[cache_key] = duration
    return duration

def find_title_root(slug: str) -> Path | None:
    # Search all MEDIA_ROOTS for a folder named after the slug
    for root in MEDIA_ROOTS:
        candidate = root / slug
        if (candidate / 'metadata.json').exists():
            return root
    return None

def get_cache_dir(folder: Path, video_path: Path | None = None) -> Path:
    # Cache lives under ./cache/media/<slug>/, mirroring the subdirectory
    # the video file lives in relative to its title folder. This means:
    #   media/backrooms/Season 1/S01E01.mkv
    #     -> cache/media/backrooms/Season 1/subtitle_S01E01.mkv_0.vtt
    #   media/oppenheimer/Oppenheimer.mkv
    #     -> cache/media/oppenheimer/scrubber_Oppenheimer.mkv.jpg
    # If no video_path is given, returns the title root cache dir.
    base = CACHE_ROOT / 'media' / folder.name
    if video_path is not None:
        try:
            rel = video_path.parent.relative_to(folder)
            base = base / rel
        except ValueError:
            pass  # video_path isn't under folder, fall back to title root
    base.mkdir(parents=True, exist_ok=True)
    return base

# So we can have different UIs for mobile, desktop, TV, and ~~Nintendo Wii~~
def get_device_type(user_agent: str) -> str:
    mobile_keywords = ['mobile', 'android', 'iphone', 'ipod', 'blackberry', 'iemobile', 'opera mini']
    tv_keywords = ['smarttv', 'hbbtv', 'appletv', 'googletv', 'netcast', 'viera', 'roku', 'dlnadoc', 'pov_tv', 'webos', 'tizen']
    wii_keywords = ['wii']  # Wii support in the big '25 is necessary
    if any(keyword in user_agent.lower() for keyword in mobile_keywords):
        device_type = 'mobile'
    elif any(keyword in user_agent.lower() for keyword in tv_keywords):
        device_type = 'tv'
    # elif any(keyword in user_agent.lower() for keyword in wii_keywords):
    #     device_type = 'wii'
    else:
        device_type = 'desktop'
    return device_type

# Functions that Jinja2 templates can call directly
def format_date(value: str | None) -> str:
    if not value:
        return '?'
    try:
        date = datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        return value
    # US uses MDY, basically everyone else uses DMY
    us_locale = locale.getlocale()[0] in (None, 'en_US', 'C', 'C.UTF-8')
    formatted_date = date.strftime('%m/%d/%Y' if us_locale else '%d/%m/%Y')
    formatted_date = re.sub(r'\b0(\d)\b', r'\1', formatted_date)  # Strip left zeros
    return formatted_date

# Sorting for the best release date to get the age rating for a movie
TYPE_PRIORITY = [3, 2, 1, 4, 5, 6]
def us_certification(release_dates: dict | None) -> str | None:
    if not release_dates:
        return None
    for entry in release_dates.get('results', []):
        if entry.get('iso_3166_1') != 'US':
            continue
        by_type = {}
        for rd in entry.get('release_dates', []):
            cert = rd.get('certification')
            if cert:
                by_type.setdefault(rd.get('type'), cert)
        for preferred_type in TYPE_PRIORITY:
            if preferred_type in by_type:
                return by_type[preferred_type]
    return None

templates = Jinja2Templates(directory=str(WWW_ROOT / 'templates'))
templates.env.filters['format_date'] = format_date
templates.env.filters['us_certification'] = us_certification

def deep_merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(base.get(key), value)
        return merged

    if isinstance(base, list) and isinstance(override, list):
        merged = list(base)
        for i, value in enumerate(override):
            if i < len(merged):
                merged[i] = deep_merge(base[i], value)
            else:
                merged.append(value)
        return merged

    return override

def _static_asset_version() -> str:
    """
    Cache-busting value derived from the newest mtime among static
    assets. Changes automatically whenever any CSS/JS file is saved,
    so browsers stop serving stale cached versions after an edit.
    """
    static_dir = WWW_ROOT / 'static'
    if not static_dir.exists():
        return '0'
    mtimes = [f.stat().st_mtime for f in static_dir.rglob('*') if f.is_file()]
    return str(int(max(mtimes))) if mtimes else '0'


# Available in every template automatically, e.g. style.css?v={{ asset_version() }}
templates.env.globals['asset_version'] = _static_asset_version

def probe_technical_info(video_path: Path) -> dict:
    """
    Uses ffprobe (via ffmpeg-python) to extract technical details for
    the details page: codecs, resolution, bitrate, file size. Returns
    an empty dict if the file can't be probed (missing file, corrupt
    header, etc.) so callers can safely merge this into data
    without special-casing failures.
    """
    if not video_path.exists():
        return {}
 
    try:
        probe = ffmpeg.probe(str(video_path))
    except ffmpeg.Error:
        return {}
 
    video_stream = next((s for s in probe['streams'] if s['codec_type'] == 'video'), None)
    audio_stream = next((s for s in probe['streams'] if s['codec_type'] == 'audio'), None)
 
    info = {}
 
    if video_stream:
        info['video_codec'] = video_stream.get('codec_name')
        width = video_stream.get('width')
        height = video_stream.get('height')
        if width and height:
            info['source_width'] = int(width)
            info['source_height'] = int(height)
            info['resolution'] = f'{width}x{height}'  # kept for the details page display
 
    if audio_stream:
        info['audio_codec'] = audio_stream.get('codec_name')
 
    format_info = probe.get('format', {})
    bit_rate = format_info.get('bit_rate')
    if bit_rate:
        info['bitrate_kbps'] = int(bit_rate) // 1000
 
    info['file_size_gb'] = round(video_path.stat().st_size / (1024 ** 3), 2)
 
    return info

def probe_subtitle_tracks(video_path: Path) -> list[dict]:
    """
    Uses ffprobe to list embedded subtitle streams in a video file,
    e.g. the multiple SRT/ASS/PGS tracks commonly muxed into an MKV.
    Returns a list of {index, language, title} dicts, where `index` is
    the subtitle stream's index *within the subtitle streams only*
    (0, 1, 2, ...) — this is what /subtitles/{slug}/{index}.vtt expects,
    not ffprobe's absolute stream index across all stream types.
    """
    if not video_path.exists():
        return []

    try:
        probe = ffmpeg.probe(str(video_path))
    except ffmpeg.Error:
        return []

    subtitle_streams = [s for s in probe['streams'] if s['codec_type'] == 'subtitle']

    tracks = []
    for i, stream in enumerate(subtitle_streams):
        tags = stream.get('tags', {})
        tracks.append({
            'index': i,
            'language': tags.get('language'),
            'title': tags.get('title'),
            # Image-based subtitle formats (e.g. PGS/VobSub, common on
            # Blu-ray rips) can't be converted to WebVTT the way we're
            # doing it here — ffmpeg's -c:s webvtt path only supports
            # text-based subtitle codecs (srt, ass, mov_text, etc.).
            'codec': stream.get('codec_name'),
        })

    return tracks

def probe_audio_tracks(video_path: Path) -> list[dict]:
    """
    Same idea as probe_subtitle_tracks, but for audio streams. index
    here is the audio stream's position among audio streams only —
    what ffmpeg's `map=0:a:N` selector and resolve_audio_stream's
    remux cache expect.
    """
    if not video_path.exists():
        return []

    try:
        probe = ffmpeg.probe(str(video_path))
    except ffmpeg.Error:
        return []

    audio_streams = [s for s in probe['streams'] if s['codec_type'] == 'audio']

    tracks = []
    for i, stream in enumerate(audio_streams):
        tags = stream.get('tags', {})
        tracks.append({
            'index': i,
            'language': tags.get('language'),
            'title': tags.get('title'),
            'codec': stream.get('codec_name'),
            'channels': stream.get('channels'),
        })
    return tracks

TEXT_SUBTITLE_CODECS = {'subrip', 'srt', 'ass', 'ssa', 'mov_text', 'webvtt'}

# Media assets (posters/backdrops) served straight from disk.
# Can't use StaticFiles here because it only accepts one directory, and
# we may have multiple media roots. This route searches all roots for
# the requested path and streams it directly.
@app.get('/media-assets/{slug}/{asset_path:path}')
def media_asset(slug: str, asset_path: str):
    root = find_title_root(slug)
    if root is None:
        raise HTTPException(status_code=404, detail='Title not found')
    full_path = root / slug / asset_path
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail='Asset not found')
    from fastapi.responses import FileResponse
    return FileResponse(str(full_path))
# Frontend static assets (css/js) served from ./www/static.
app.mount('/www/static', StaticFiles(directory=str(WWW_ROOT / 'static')), name='www-static')

def load_title(slug: str):
    """
    Load and parse a single title's data by slug, merging
    tmdb_metadata.json (fallback data) under metadata.json (source of truth).
    Used by every route that needs one title's data, so the merge
    logic lives in exactly one place instead of being copy-pasted
    (and drifting out of sync) across routes.

    Raises HTTPException(404) if the title folder/metadata.json doesn't
    exist, or HTTPException(500) if the JSON is malformed or fails
    validation.
    """
    root = find_title_root(slug)
    if root is None:
        raise HTTPException(status_code=404, detail='Title not found')
    folder = root / slug
    metadata_path = folder / 'metadata.json'
    tmdb_metadata_path = folder / 'tmdb_metadata.json'

    if not metadata_path.exists():
        raise HTTPException(status_code=404, detail='Title not found')

    try:
        own_data = json.loads(metadata_path.read_text(encoding='utf-8'))
        tmdb_fallback = json.loads(tmdb_metadata_path.read_text(encoding='utf-8')) if tmdb_metadata_path.exists() else {}
        raw = deep_merge(tmdb_fallback, own_data)
        parsed = parse_title_data(raw)
        if parsed.media_type == 'movie':
            if parsed.sort_title is None and parsed.title is not None:
                parsed.sort_title = parsed.title
        elif parsed.media_type == 'tv':
            if parsed.sort_title is None and parsed.name is not None:
                parsed.sort_title = parsed.name
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f'Malformed JSON for {slug}: {e}')
    except ValidationError as e:
        raise HTTPException(status_code=500, detail=f'Invalid data for {slug}: {e}')

    return folder, parsed

def scan_library(sort: bool = False) -> list[dict]:
    """
    Walk all MEDIA_ROOTS for title folders, each expected to contain a
    metadata.json. Folder name (slug) is used only for building asset
    URLs — it's not the title's identity, the JSON is.

    Returns plain dicts (via model_dump) rather than Pydantic model
    instances, since Jinja2 templates read attributes the same either
    way and dicts keep the existing template code (t.title, t._slug)
    working unchanged.
    """
    titles = []
    seen_slugs: set[str] = set()

    all_folders = []
    for root in MEDIA_ROOTS:
        if not root.exists():
            continue
        for folder in root.iterdir():
            if not folder.is_dir():
                continue
            if folder.name in seen_slugs:
                # Duplicate slug across roots - first root wins, same as
                # find_title_root. Log a warning so it's not silent.
                log(type='warning', message=f'Duplicate slug "{folder.name}" in {root}, skipping')
                continue
            seen_slugs.add(folder.name)
            all_folders.append(folder)

    for folder in all_folders:
        try:
            _, parsed = load_title(folder.name)
        except HTTPException:
            continue  # Skip malformed/missing entries rather than crashing the whole scan

        if parsed.media_type == 'movie':
            # Get movie length via ffprobe, if possible
            movie_path = folder / parsed.movie_path
            if movie_path.exists():
                duration = get_cached_duration(movie_path)
                if duration is not None:
                    parsed.movie_length = duration
        elif parsed.media_type == 'tv':
            # Handle TV series data
            pass

        data = parsed.model_dump()
        data['_slug'] = folder.name
        titles.append(data)

    # Sort titles by sort_title in the data, falling back to title if sort_title is missing.
    if sort:
        titles.sort(key=lambda t: t.get('sort_title') or t.get('title') or t.get('name') or '')

    return titles

def pick_random_titles(count: int, filter: dict | None = None, titles_batch=dict) -> list[dict]:
    """
    Picks a random sample of titles from the library, up to `count`.
    Returns plain dicts (via model_dump) rather than Pydantic model
    instances, since Jinja2 templates read attributes the same either
    way and dicts keep the existing template code (t.title, t._slug)
    working unchanged.
    """
    if filter:
        titles_batch = [t for t in titles_batch if all(t.get(k) == v for k, v in filter.items())]
    if len(titles_batch) <= count:
        return titles_batch

    sampled = random.sample(titles_batch, count)
    return sampled

def get_continue_watching(watch_history: list[dict], limit: int = 16) -> list[dict]:
    # Group by slug, keep only the most recently accessed entry per slug
    by_slug = {}
    for entry in watch_history:
        slug = entry['slug']
        if slug not in by_slug or entry['accessed'] > by_slug[slug]['accessed']:  # Keep the most recent entry for each slug (for shows)
            by_slug[slug] = entry

        # Get title data for each entry, letting the watch-history entry's own
        # fields (timestamp, season_number, episode_number, accessed, etc.)
        # win over anything with the same name on the title metadata model.
        try:
            folder, parsed = load_title(slug)
            title_data = parsed.model_dump()
            title_data.update(entry)
            entry.clear()
            entry.update(title_data)
        except HTTPException:
            continue  # Skip malformed/missing entries rather than crashing the whole scan

        # Get full backdrop path for each entry, falling back to TMDB if the local file doesn't exist
        if entry.get('backdrop_path'):
            root = find_title_root(slug)
            backdrop_path = (root / slug / entry['backdrop_path']) if root else None
            if backdrop_path and backdrop_path.exists():
                entry['backdrop_path_full'] = f'/media-assets/{slug}/{entry["backdrop_path"]}'
            else:
                # Fallback to TMDB backdrop image if the local file doesn't exist
                entry['backdrop_path_full'] = f'https://image.tmdb.org/t/p/original{entry.get("backdrop_path")}'

    for entry in by_slug.values():
        # Get the movie or episode length for each entry in seconds using ffprobe
        if entry.get('media_type') == 'movie':
            folder = find_title_root(entry['slug'])
            if folder:
                movie_path = folder / entry['slug'] / entry.get('movie_path', '')
                if movie_path.exists():
                    try:
                        probe = ffmpeg.probe(str(movie_path))
                        duration = float(probe['format']['duration'])
                        entry['movie_length'] = int(duration)
                    except Exception:
                        pass  # Don't fail the whole scan if ffprobe fails for some reason

        elif entry.get('media_type') == 'tv':
            folder = find_title_root(entry['slug'])
            if folder:
                season_number = entry.get('season_number')
                episode_number = entry.get('episode_number')
                if season_number is not None and episode_number is not None:
                    try:
                        # Search by season_number/episode_number field, same as
                        # /player and /details — never by list position, since
                        # season/episode numbering isn't guaranteed to match index.
                        _, parsed = load_title(entry['slug'])
                        season_obj = next((s for s in parsed.seasons or [] if s.season_number == season_number), None)
                        if season_obj:
                            episode_obj = next(
                                (e for e in season_obj.episodes if e.episode_number == episode_number), None
                            )
                            if episode_obj and episode_obj.episode_path:
                                entry['episode_name'] = episode_obj.name
                                episode_path = folder / entry['slug'] / episode_obj.episode_path
                                if episode_path.exists():
                                    try:
                                        probe = ffmpeg.probe(str(episode_path))
                                        duration = float(probe['format']['duration'])
                                        entry['episode_length'] = int(duration)
                                    except Exception:
                                        pass  # Don't fail the whole scan if ffprobe fails for some reason

                                # Still image, same fallback pattern as /details
                                still_path_value = episode_obj.still_path or ''
                                still_path = (folder / entry['slug'] / still_path_value) if still_path_value else None
                                if still_path and still_path.exists():
                                    entry['still_path_full'] = f'/media-assets/{entry["slug"]}/{episode_obj.still_path}'
                                else:
                                    entry['still_path_full'] = f'https://image.tmdb.org/t/p/original{episode_obj.still_path or ""}'
                    except HTTPException:
                        continue  # Skip malformed/missing entries rather than crashing the whole scan

    most_recent = list(by_slug.values())
    most_recent.sort(key=lambda e: e['accessed'], reverse=True)
    return most_recent[:limit]

def record_watch_progress(watch_history: list[dict], slug: str, media_type: str,
                           timestamp: int, accessed: str,
                           season_number: int | None = None, episode_number: int | None = None) -> list[dict]:
    for entry in watch_history:
        if (entry['slug'] == slug
                and entry.get('season_number') == season_number
                and entry.get('episode_number') == episode_number):
            entry['timestamp'] = timestamp
            entry['accessed'] = accessed
            return watch_history

    # Not found, add new entry
    new_entry = {'slug': slug, 'media_type': media_type, 'timestamp': timestamp, 'accessed': accessed}
    if season_number is not None:
        new_entry['season_number'] = season_number
        new_entry['episode_number'] = episode_number
    watch_history.append(new_entry)
    return watch_history

def resolve_episode_path(folder: Path, parsed, season: int | None = None, episode: int | None = None) -> Path:
    if parsed.media_type == 'movie':
        if not parsed.movie_path:
            raise HTTPException(status_code=404, detail='No video file configured for this title')
        return folder / parsed.movie_path

    # media_type == 'tv'
    if season is None or episode is None:
        raise HTTPException(status_code=400, detail='season and episode query params are required for TV titles')

    season_obj = next((s for s in parsed.seasons or [] if s.season_number == season), None)
    if season_obj is None:
        raise HTTPException(status_code=404, detail=f'Season {season} not found')

    episode_obj = next((e for e in season_obj.episodes if e.episode_number == episode), None)
    if episode_obj is None:
        raise HTTPException(status_code=404, detail=f'Episode {episode} of season {season} not found')

    if not episode_obj.episode_path:
        raise HTTPException(status_code=404, detail=f'No video file configured for S{season}E{episode}')

    return folder / episode_obj.episode_path

def resolve_audio_stream(folder: Path, video_path: Path, audio_index: int) -> Path:
    """
    SUPERSEDED — this wrote a full copy of the entire video to disk for every
    non-default audio track (20 GB file = 20 GB cache entry, oops).

    Audio track switching now uses transcode.build_remux_cmd(), which pipes
    ffmpeg stream-copy output directly to the client with no on-disk cache.
    This function is kept here only for reference and is no longer called.
    """
    cache_path = get_cache_dir(folder) / f'audio_{video_path.stem}_{audio_index}.mp4'
    if cache_path.exists():
        return cache_path

    result = subprocess.run(
        [
            'ffmpeg', '-i', str(video_path),
            '-map', '0:v:0', '-map', f'0:a:{audio_index}',
            '-c', 'copy',
            str(cache_path),
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f'Failed to remux audio track: {result.stderr.decode(errors="replace")}')

    return cache_path

@app.middleware('http')
async def require_token(request: Request, call_next):
    # Runs before each request to check the token, and re-sets the
    # cookie's expiration on every request so an active viewer's
    # session never expires mid-use (sliding expiration). Only idle
    # sessions (no requests for COOKIE_MAX_AGE) will time out.
    #
    # If require_token is off in config.jsonc, skip the token wall
    # entirely (e.g. for a private home server on a trusted network).

    if not cfg_require_token:
        return await call_next(request)

    path = request.url.path

    is_public = path in PUBLIC_PATHS or path.startswith(PUBLIC_PATH_PREFIXES)
    if is_public:
        return await call_next(request)

    token = request.cookies.get(TOKEN_COOKIE_NAME)
    if not is_valid_token(token):
        return RedirectResponse(url='/access')

    response = await call_next(request)

    # Reissue the cookie with a fresh max_age so it keeps sliding
    # forward as long as the person keeps visiting pages. `secure`
    # must match the scheme actually used — browsers silently drop
    # Secure cookies set over plain HTTP (e.g. local dev on
    # http://localhost), which otherwise causes an infinite redirect
    # loop back to /access.
    response.set_cookie(
        key=TOKEN_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='lax',
        max_age=COOKIE_MAX_AGE,
    )

    return response

@app.get('/access')
def login_page(request: Request):
    device_type = get_device_type(request.headers.get('User-Agent', ''))
    return templates.TemplateResponse(request=request, name='token.html', context={'device_type': device_type})
 
 
@app.post('/access')
def login_submit(request: Request, token: str = Form('')):
    if not is_valid_token(token):
        return templates.TemplateResponse(
            request=request, name='token.html',
            context={'error': 'Invalid token.'}, status_code=401,
        )
 
    response = RedirectResponse(url='/', status_code=303)
    response.set_cookie(
        key=TOKEN_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=request.url.scheme == 'https',
        samesite='lax',
        max_age=COOKIE_MAX_AGE,
    )
    return response

@app.get('/')
def index(request: Request):
    # Account data
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    logged_in_account = accounts.get_account_from_session(session_token) if session_token else None

    all_titles = scan_library(sort=False)
    titles = pick_random_titles(count=32, titles_batch=all_titles)
    titles_movies = pick_random_titles(count=32, filter={'media_type': 'movie'}, titles_batch=all_titles)
    titles_tv = pick_random_titles(count=32, filter={'media_type': 'tv'}, titles_batch=all_titles)
    # Get full paths for poster images, falling back to TMDB if the local file doesn't exist
    for t in titles + titles_movies + titles_tv:
        root = find_title_root(t['_slug'])
        poster_path = (root / t['_slug'] / t.get('poster_path', '')) if root else None
        if poster_path and poster_path.exists():
            t['poster_path_full'] = f'/media-assets/{t["_slug"]}/{t["poster_path"]}'
        else:
            # Fallback to TMDB poster image if the local file doesn't exist
            t['poster_path_full'] = f'https://image.tmdb.org/t/p/original{t.get("poster_path")}'
    random.shuffle(titles)
    random.shuffle(titles_movies)
    random.shuffle(titles_tv)

    watch_history = get_continue_watching(accounts.get_watch_history(logged_in_account['id']) if logged_in_account else [])

    device_type = get_device_type(request.headers.get('User-Agent', ''))
    return templates.TemplateResponse(
        request=request, name='index.html', context={
            'titles': titles,
            'titles_movies': titles_movies,
            'titles_tv': titles_tv,
            'watch_history': watch_history,
            'account': logged_in_account,
            'device_type': device_type
        }
    )

@app.get('/alphabetical')
def alphabetical_index(request: Request):
    device_type = get_device_type(request.headers.get('User-Agent', ''))
    titles = scan_library(sort=True)

    for t in titles:
        root = find_title_root(t['_slug'])
        poster_path = (root / t['_slug'] / t.get('poster_path', '')) if root else None
        if poster_path and poster_path.exists():
            t['poster_path_full'] = f'/media-assets/{t["_slug"]}/{t["poster_path"]}'
        else:
            t['poster_path_full'] = f'https://image.tmdb.org/t/p/original{t.get("poster_path")}'

    # Group titles by first letter server-side
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    grouped = {}
    for letter in alphabet:
        matches = [t for t in titles if (t.get('sort_title') or t.get('title') or t.get('name') or '').upper().startswith(letter)]
        if matches:
            grouped[letter] = matches
    # Titles starting with a number or symbol
    other = [t for t in titles if not ( t.get('sort_title') or t.get('title') or t.get('name') or '').upper()[:1].isalpha()]
    if other:
        grouped['#'] = other

    return templates.TemplateResponse(
        request=request, name='alphabetical.html',
        context={'grouped': grouped, 'device_type': device_type}
    )

@app.get('/account')
def account(request: Request):
    device_type = get_device_type(request.headers.get('User-Agent', ''))
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    logged_in_account = accounts.get_account_from_session(session_token) if session_token else None

    # Get data for logged in accounts
    lang_codes = []
    account_settings = {}
    if logged_in_account is not None:
        lang_codes = sorted(ISO_639_2_NAMES.items(), key=lambda x: x[1])  # Sort by bibligraphic name for display in the dropdown
        account_settings = accounts.get_settings(logged_in_account['id'])

    return templates.TemplateResponse(
        request=request, name='account.html',
        context={
            'device_type': device_type,
            'lang_codes': lang_codes,
            'account_settings': account_settings,
            'account': logged_in_account
        },
    )

@app.post('/account/register')
def account_register(request: Request, username: str = Form(...), password: str = Form(...)):
    device_type = get_device_type(request.headers.get('User-Agent', ''))

    username = username.strip()
    if not username or not password:
        return templates.TemplateResponse(
            request=request, name='account.html',
            context={'error': 'Username and password are required.'}, status_code=400,
        )

    account_id = accounts.create_account(username, password)
    if account_id is None:
        return templates.TemplateResponse(
            request=request, name='account.html',
            context={'device_type': device_type, 'error': 'That username is already taken.'}, status_code=409,
        )

    return _log_in_response(account_id)

@app.post('/account/login')
def account_login(request: Request, username: str = Form(...), password: str = Form(...)):
    device_type = get_device_type(request.headers.get('User-Agent', ''))
    account_id = accounts.verify_login(username.strip(), password)
    if account_id is None:
        # Deliberately vague: don't reveal whether the username exists,
        # so the error can't be used to enumerate registered usernames.
        return templates.TemplateResponse(
            request=request, name='account.html',
            context={'device_type': device_type, 'error': 'Incorrect username or password.'}, status_code=401,
        )

    return _log_in_response(account_id)

@app.post('/account/update-settings')
async def account_update_settings(request: Request):
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        return RedirectResponse(url='/account', status_code=303)

    account = accounts.get_account_from_session(session_token)
    if not account:
        return RedirectResponse(url='/account', status_code=303)

    json_data = await request.json()
    accounts.update_settings(account['id'], json_data)
    return {'status': 'ok'}

@app.post('/account/delete')
def account_delete(request: Request):
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_token:
        return RedirectResponse(url='/account', status_code=303)

    account = accounts.get_account_from_session(session_token)
    if not account:
        return RedirectResponse(url='/account', status_code=303)

    accounts.delete_account(account['id'])
    response = RedirectResponse(url='/account', status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response

@app.post('/account/logout')
def account_logout(request: Request):
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token:
        accounts.delete_session(session_token)

    response = RedirectResponse(url='/account', status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response

def _log_in_response(account_id: int) -> RedirectResponse:
    """Shared by register/login: issues a session cookie and redirects home."""
    session_token = accounts.create_session(account_id)
    response = RedirectResponse(url='/', status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=False,  # Set True once served over https, same as TOKEN_COOKIE_NAME's cookie
        samesite='lax',
        max_age=COOKIE_MAX_AGE,
    )
    return response

@app.get('/search')
def search(request: Request, q: str = Query('')):
    device_type = get_device_type(request.headers.get('User-Agent', ''))

    all_titles = scan_library(sort=False)
    # Use rapidfuzz to find titles that match the query string, ignoring case and punctuation
    matching_titles = []
    for t in all_titles:
        title_to_check = (t.get('sort_title') or t.get('title') or t.get('name') or '').lower()
        if rapidfuzz.fuzz.partial_ratio(q.lower(), title_to_check) >= 70:
            matching_titles.append(t)

    for t in matching_titles:
        root = find_title_root(t['_slug'])
        poster_path = (root / t['_slug'] / t.get('poster_path', '')) if root else None
        if poster_path and poster_path.exists():
            t['poster_path_full'] = f'/media-assets/{t["_slug"]}/{t["poster_path"]}'
        else:
            t['poster_path_full'] = f'https://image.tmdb.org/t/p/original{t.get("poster_path")}'
        # Get the movie length for each matching title if it's a movie
        if t.get('media_type') == 'movie':
            folder = find_title_root(t['_slug'])
            if folder:
                movie_path = folder / t['_slug'] / t.get('movie_path', '')
                if movie_path.exists():
                    duration = get_cached_duration(movie_path)
                    if duration is not None:
                        t['movie_length'] = duration

    return templates.TemplateResponse(
        request=request, name='search.html',
        context={'query': q, 'results': matching_titles, 'device_type': device_type}
    )

@app.get('/details/{slug}')
def details(request: Request, slug: str, season: int | None = None):
    folder, parsed = load_title(slug)

    metadata = parsed.model_dump()

    # TODO: Make checking movie length not redundant code
    if parsed.media_type == 'movie':
        tech_info = probe_technical_info(folder / parsed.movie_path)
        metadata.update({k: v for k, v in tech_info.items() if v is not None})

        movie_path = folder / parsed.movie_path
        duration = get_cached_duration(movie_path)
        if duration is not None:
            metadata['movie_length'] = duration

    # Get full paths for poster and backdrop, falling back to TMDB if the local file doesn't exist
    _asset_root = find_title_root(slug)
    poster_path = (_asset_root / slug / metadata.get('poster_path', '')) if _asset_root else None
    if poster_path and poster_path.exists():
        metadata['poster_path_full'] = f'/media-assets/{slug}/{metadata["poster_path"]}'
    else:
        metadata['poster_path_full'] = f'https://image.tmdb.org/t/p/original{metadata.get("poster_path")}'
    backdrop_path = (_asset_root / slug / metadata.get('backdrop_path', '')) if _asset_root else None
    if backdrop_path and backdrop_path.exists():
        metadata['backdrop_path_full'] = f'/media-assets/{slug}/{metadata["backdrop_path"]}'
    else:
        metadata['backdrop_path_full'] = f'https://image.tmdb.org/t/p/original{metadata.get("backdrop_path")}'

    # Default to the first season if none was requested or the requested season doesn't exist
    selected_season = season
    if metadata.get('seasons'):
        seasons_with_episodes = [
            s for s in metadata['seasons']
            if any(e.get('episode_path') is not None for e in s.get('episodes', []))
        ]
        valid_numbers = [s['season_number'] for s in seasons_with_episodes]
        if selected_season not in valid_numbers:
            selected_season = valid_numbers[0] if valid_numbers else None

    # For each episode with an episode path, get the runtime and add it to the data
    if metadata.get('seasons'):
        def probe_episode(season_obj, episode_obj):
            episode_path = folder / episode_obj['episode_path']
            duration = get_cached_duration(episode_path)
            if duration is not None:
                episode_obj['length'] = duration

        episodes_to_probe = [
            (season_obj, episode_obj)
            for season_obj in metadata['seasons']
            for episode_obj in season_obj.get('episodes', [])
            if episode_obj.get('episode_path')
        ]

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(probe_episode, s, e) for s, e in episodes_to_probe]
            for f in as_completed(futures):
                pass  # Results written directly into episode_obj dicts

    # Get still paths for each episode, falling back to TMDB if the local file doesn't exist
    for season_obj in metadata.get('seasons', []):
        for episode_obj in season_obj.get('episodes', []):
            still_path_value = episode_obj.get('still_path') or ''
            still_path = (_asset_root / slug / still_path_value) if _asset_root and still_path_value else None
            if still_path and still_path.exists():
                episode_obj['still_path_full'] = f'/media-assets/{slug}/{episode_obj["still_path"]}'
            else:
                episode_obj['still_path_full'] = f'https://image.tmdb.org/t/p/original{episode_obj.get("still_path") or ""}'

    # Account stuff for watch history
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    logged_in_account = accounts.get_account_from_session(session_token) if session_token else None

    # Add the timestamp and episode_length for each episode in the watch history, if available.
    full_watch_history = accounts.get_watch_history(logged_in_account['id']) if logged_in_account else []
    for season_obj in metadata.get('seasons', []):
        for episode_obj in season_obj.get('episodes', []):
            episode_obj['timestamp'] = None
            episode_obj['accessed'] = None
            for entry in full_watch_history:
                if (entry['slug'] == slug and entry.get('season_number') == season_obj['season_number']
                        and entry.get('episode_number') == episode_obj['episode_number']):
                    if entry.get('timestamp') is not None:
                        episode_obj['timestamp'] = entry.get('timestamp')
                    episode_obj['accessed'] = entry.get('accessed')
                    if 'length' not in episode_obj:
                        episode_path = folder / episode_obj['episode_path']
                        duration = get_cached_duration(episode_path)
                        if duration is not None:
                            episode_obj['length'] = duration
                    break

    # For movies, get just the timestamp and accessed from the watch history, since there's no season/episode structure
    timestamp = None
    accessed = None
    if parsed.media_type == 'movie':
        for entry in full_watch_history:
            if entry['slug'] == slug:
                timestamp = entry.get('timestamp')
                accessed = entry.get('accessed')
                break
        metadata['timestamp'] = timestamp
        metadata['accessed'] = accessed
    watch_data = {
        'timestamp': timestamp,
        'accessed': accessed,
    }

    device_type = get_device_type(request.headers.get('User-Agent', ''))
    return templates.TemplateResponse(
        request=request, name='details.html',
        context={'metadata': metadata, 'slug': slug, 'selected_season': selected_season, 'watch_data': watch_data, 'device_type': device_type},
    )

@app.get('/download/{slug}')
def download(slug: str, season: int | None = None, episode: int | None = None):
    folder, parsed = load_title(slug)

    if parsed.media_type == 'movie':
        video_path = folder / parsed.movie_path
        if not video_path.exists():
            raise HTTPException(status_code=404, detail=f'Video file not found at {video_path}')
    if parsed.media_type == 'tv':
        if season is None or episode is None:
            raise HTTPException(status_code=400, detail='season and episode query params are required for TV titles')
        video_path = resolve_episode_path(folder, parsed, season, episode)
        if not video_path.exists():
            raise HTTPException(status_code=404, detail=f'Video file not found at {video_path}')
        video_path = video_path

    return FileResponse(
        str(video_path),
        media_type='application/octet-stream',
        filename=video_path.name,
    )

@app.get('/player/{slug}')
def player_page(request: Request, slug: str, season: int | None = None, episode: int | None = None, timestamp: int | None = None):
    folder, parsed = load_title(slug)

    metadata = parsed.model_dump()

    current_episode = None
    if parsed.media_type == 'tv' and season is not None and episode is not None:
        season_obj = next((s for s in parsed.seasons or [] if s.season_number == season), None)
        if season_obj:
            current_episode = next((e for e in season_obj.episodes if e.episode_number == episode), None)

    video_path = None
    if parsed.media_type == 'movie':
        video_path = folder / parsed.movie_path
    elif parsed.media_type == 'tv':
        video_path = resolve_episode_path(folder, parsed, season, episode)

    subtitle_tracks = []
    audio_tracks = []
    is_transcoded = False
    media_duration_seconds = None
    if video_path.exists():
        all_subtitle_tracks = probe_subtitle_tracks(video_path)
        subtitle_tracks = [t for t in all_subtitle_tracks if t['codec'] in TEXT_SUBTITLE_CODECS]
        audio_tracks = probe_audio_tracks(video_path)

        technical = probe_technical_info(video_path)
        needed = transcode.needs_transcode(technical.get('video_codec'), technical.get('audio_codec'))
        is_transcoded = needed['video'] or needed['audio']

        # Get video length
        try:
            media_duration_seconds = get_cached_duration(video_path)
        except Exception:
            log(type='warning', message=f'Failed to probe duration for {video_path}')
            media_duration_seconds = None

    scrubber_meta = None
    scrubber_json = get_cache_dir(folder, video_path) / f'scrubber_{video_path.name}.json'
    if scrubber_json.exists():
        scrubber_meta = json.loads(scrubber_json.read_text())

    device_type = get_device_type(request.headers.get('User-Agent', ''))

    return templates.TemplateResponse(
        request=request, name='player.html',
        context={
            'metadata': metadata, 'slug': slug, 'season': season, 'episode': episode,
            'current_episode': current_episode.model_dump() if current_episode else None,
            'subtitle_tracks': subtitle_tracks, 'audio_tracks': audio_tracks,
            'is_transcoded': is_transcoded, 'media_duration_seconds': media_duration_seconds,
            'scrubber_meta': scrubber_meta,
            'device_type': device_type,
        },
    )

@app.get('/scrubber/{slug}')
def scrubber_image(slug: str, season: int | None = None, episode: int | None = None):
    folder, parsed = load_title(slug)
    video_path = None
    if parsed.media_type == 'movie':
        video_path = folder / parsed.movie_path
        if not video_path.exists():
            raise HTTPException(status_code=404, detail=f'Video file not found at {video_path}')
    if parsed.media_type == 'tv':
        video_path = resolve_episode_path(folder, parsed, season, episode)
    jpg = get_cache_dir(folder, video_path) / f'scrubber_{video_path.name}.jpg'
    if not jpg.exists():
        raise HTTPException(status_code=404)
    return FileResponse(jpg, media_type='image/jpeg')

@app.get('/stream/{slug}')
def stream(
    slug: str, request: Request, season: int | None = None, episode: int | None = None,
    audio: int | None = None, t: float = 0.0,
):
    folder, parsed = load_title(slug)

    episode_path = resolve_episode_path(folder, parsed, season, episode)
    if not episode_path.exists():
        raise HTTPException(status_code=404, detail=f'Video file not found at {episode_path}')

    audio_index = audio if audio is not None else 0
    technical = probe_technical_info(episode_path)
    audio_tracks = probe_audio_tracks(episode_path)
    needed = transcode.needs_transcode(technical.get('video_codec'), audio_tracks[audio_index]['codec'] if audio_index < len(audio_tracks) else None)

    # Live transcode path: the source has HEVC video and/or EAC3/AC3
    # audio, neither of which browsers play natively. There's no
    # Content-Length here (we don't know the transcoded size ahead of
    # time) and no byte-range seeking - ffmpeg is generating bytes as
    # it goes, so "seek to byte offset N" is meaningless. Instead the
    # frontend sends ?t=<seconds> on scrub, and we restart ffmpeg with
    # -ss at that timestamp. This intentionally does NOT reuse the
    # audio-track remux/cache path below, since transcoded output
    # can't be cached the same way (or at all, cheaply) as a stream-copy.
    if needed['video'] or needed['audio']:
        cmd, video_encoder = transcode.build_transcode_cmd(
            episode_path, seek_seconds=t, audio_index=audio_index,
            transcode_video=needed['video'], transcode_audio=needed['audio'],
            source_width=technical.get('source_width'),
            source_height=technical.get('source_height'),
        )

        # A small chunk size for live transcode output is deliberate here.
        # proc.stdout.read(n)
        # blocks until either n bytes are available or the pipe hits
        # EOF - for a plain file that's irrelevant since the OS can
        # satisfy a 64MB read from disk almost instantly, but for a
        # *live* ffmpeg process trickling out encoded output, a 64MB
        # read effectively means "wait until ffmpeg has produced 64MB
        # or finished," which for a feature-length file is minutes of
        # apparent freeze with zero bytes reaching the browser. 64KB
        # keeps bytes flowing to the client as fast as ffmpeg makes them.
        TRANSCODE_CHUNK_SIZE = 64 * 1024

        # A cached "this encoder works" result came from a cheap
        # synthetic probe (a few black frames), which can pass while
        # the real thing still fails - e.g. NVENC opening fine for a
        # tiny test clip but returning "No capable devices found" once
        # asked to actually decode a real 2160p HDR frame and open a
        # genuine encode session. So before committing to a
        # StreamingResponse (after which headers are already sent and
        # we can no longer swap encoders), start ffmpeg here and read
        # just enough to confirm real output is actually flowing. If
        # it isn't, demote the encoder and rebuild the command once -
        # this keeps a broken GPU from turning into a dead player on
        # every single request, while still only paying this
        # startup-verification cost when something's actually broken
        # (a healthy encoder produces its first chunk almost
        # immediately, so this adds negligible latency in the normal case).
        def start_transcode_process(cmd):
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stderr_lines = []

            def drain_stderr():
                for line in proc.stderr:
                    stderr_lines.append(line)

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()
            return proc, stderr_lines, stderr_thread

        proc, stderr_lines, stderr_thread = start_transcode_process(cmd)
        first_chunk = proc.stdout.read(TRANSCODE_CHUNK_SIZE)

        if not first_chunk:
            # No output at all before EOF - almost certainly an encoder
            # failure rather than a legitimately empty file. Confirm
            # ffmpeg actually exited (vs. just being slow to produce
            # the first fragment) and, if so, demote and retry once.
            proc.wait(timeout=5)
            stderr_thread.join(timeout=2)
            if proc.returncode != 0 and needed['video']:
                stderr_output = b''.join(stderr_lines).decode(errors='replace')
                log(type='error', message=f'Transcode failed with {video_encoder} for {episode_path}: {stderr_output}')
                transcode.demote_broken_encoder(video_encoder)

                cmd, video_encoder = transcode.build_transcode_cmd(
                    episode_path, seek_seconds=t, audio_index=audio_index,
                    transcode_video=needed['video'], transcode_audio=needed['audio'],
                    source_width=technical.get('source_width'),
                    source_height=technical.get('source_height'),
                )
                proc, stderr_lines, stderr_thread = start_transcode_process(cmd)
                first_chunk = proc.stdout.read(TRANSCODE_CHUNK_SIZE)

            if not first_chunk:
                # Retried (or wasn't a video-encoder issue to begin
                # with) and still got nothing - this is a real failure,
                # not a stale-cache problem. Surface it properly
                # instead of returning a 200 with an empty body that
                # leaves the player silently frozen.
                proc.wait(timeout=5)
                stderr_thread.join(timeout=2)
                stderr_output = b''.join(stderr_lines).decode(errors='replace')
                log(type='error', message=f'Transcode failed for {episode_path}: {stderr_output}')
                raise HTTPException(status_code=500, detail='Transcoding failed - check server logs')

        # Logged on every successful start, not just failures - this is
        # the only way to actually know which encoder is running for a
        # given request. A request can "work" while quietly running on
        # slow software libx264 (e.g. because NVENC failed and got
        # silently demoted moments earlier by a *different* request),
        # and without this line that's indistinguishable from NVENC
        # itself just being slow on a particular file.
        log(type='info', message=f'Transcoding {episode_path} with video_encoder={video_encoder}')

        def transcode_iter():
            try:
                if first_chunk:
                    yield first_chunk
                while chunk := proc.stdout.read(TRANSCODE_CHUNK_SIZE):
                    yield chunk
                proc.wait()
                stderr_thread.join(timeout=2)
                if proc.returncode != 0:
                    stderr_output = b''.join(stderr_lines).decode(errors='replace')
                    log(type='error', message=f'Transcode ended with error for {episode_path}: {stderr_output}')
            finally:
                # If the client disconnects mid-stream (closes the tab,
                # scrubs again triggering a new request), the generator
                # stops being iterated and we land here - without this,
                # the ffmpeg process would keep encoding into a pipe
                # nobody's reading, forever.
                proc.stdout.close()
                proc.terminate()
                proc.wait()

        headers = {
            'Content-Type': 'video/mp4',
            'Accept-Ranges': 'none',  # We don't support byte-range seeking on this path
        }
        return StreamingResponse(transcode_iter(), status_code=200, headers=headers)

    # Remux path: non-default audio track, OR a time-seek on a non-transcoded file.
    #
    # The second condition (t > 0) covers switching *back* to track 0 from a
    # non-zero track: once the JS enters remux mode it keeps using ?t= for all
    # subsequent seeks, so we can't fall through to the byte-range static path
    # (we'd have no idea which byte offset corresponds to t seconds).
    #
    # Stream-copy: ffmpeg picks the requested streams and muxes them into a
    # fragmented MP4 piped straight to the client — no intermediate file written,
    # no 20 GB surprises for multilingual MKVs.
    if (audio is not None and audio != 0) or t > 0:
        cmd = transcode.build_remux_cmd(episode_path, seek_seconds=t, audio_index=audio_index)
        log(type='info', message=f'Remuxing {episode_path} audio_index={audio_index} t={t:.2f}')

        REMUX_CHUNK_SIZE = 64 * 1024
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stderr_lines = []

        def drain_remux_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=drain_remux_stderr, daemon=True)
        stderr_thread.start()

        def remux_iter():
            try:
                while chunk := proc.stdout.read(REMUX_CHUNK_SIZE):
                    yield chunk
                proc.wait()
                stderr_thread.join(timeout=2)
                if proc.returncode != 0:
                    stderr_output = b''.join(stderr_lines).decode(errors='replace')
                    log(type='error', message=f'Remux ended with error for {episode_path}: {stderr_output}')
            finally:
                proc.stdout.close()
                proc.terminate()
                proc.wait()

        headers = {
            'Content-Type': 'video/mp4',
            'Accept-Ranges': 'none',
        }
        return StreamingResponse(remux_iter(), status_code=200, headers=headers)

    # Static serve path: browser-native codec (e.g. H264), default audio, no
    # time-seek. FileResponse handles range requests natively and uses async
    # I/O — avoiding the per-chunk thread-pool dispatch that the old sync
    # generators (full_iter / ranged_iter) incurred. That dispatch overhead was
    # the root cause of stream freezes on high-latency media like USB drives:
    # every 2 MB chunk paid a USB read + a thread pool round-trip, whereas
    # FileResponse amortises that cost with its own async buffering.
    return FileResponse(str(episode_path), media_type='video/mp4')

@app.get('/subtitles/{slug}/{track_index}.vtt')
def subtitle_track(slug: str, track_index: int, season: int | None = None, episode: int | None = None):
    """
    Extracts one embedded subtitle stream and converts it to WebVTT
    on the fly, so the browser's native <track> element can render
    it. track_index is the subtitle's position among subtitle streams
    only (0, 1, 2, ...) — matching what probe_subtitle_tracks()
    reports, not ffprobe's absolute stream index across all stream
    types.

    The extracted VTT is cached to disk on first request, since
    running ffmpeg synchronously on every track selection was the
    main source of the "subtitles take a while (or never) show up"
    delay — video files don't change once ingested, so this is safe.
    """
    folder, parsed = load_title(slug)

    video_path = resolve_episode_path(folder, parsed, season, episode)
    if not video_path.exists():
        raise HTTPException(status_code=404, detail=f'Video file not found at {video_path}')

    cache_path = get_cache_dir(folder, video_path) / f'subtitle_{video_path.name}_{track_index}.vtt'
    if cache_path.exists():
        return StreamingResponse(iter([cache_path.read_bytes()]), media_type='text/vtt')

    tracks = probe_subtitle_tracks(video_path)
    if track_index < 0 or track_index >= len(tracks):
        raise HTTPException(status_code=404, detail='Subtitle track not found')

    if tracks[track_index]['codec'] not in TEXT_SUBTITLE_CODECS:
        raise HTTPException(
            status_code=400,
            detail=f'Subtitle track {track_index} uses an image-based codec '
                   f'({tracks[track_index]["codec"]}) that can\'t be converted to WebVTT',
        )

    try:
        out, _ = (
            ffmpeg
            .input(str(video_path))
            .output('pipe:', map=f'0:s:{track_index}', f='webvtt')
            .run(capture_stdout=True, capture_stderr=True)
        )
    except ffmpeg.Error as e:
        raise HTTPException(status_code=500, detail=f'Failed to extract subtitle track: {e.stderr.decode(errors="replace")}')

    cache_path.write_bytes(out)
    return StreamingResponse(iter([out]), media_type='text/vtt')

@app.get('/monitor')
async def server_monitor(request: Request):
    info = await run_in_threadpool(monitor.get_system_info)
    device_type = get_device_type(request.headers.get('User-Agent', ''))
    return templates.TemplateResponse(
        request=request, name='monitor.html',
        context={'server_monitor': info, 'device_type': device_type},
    )

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.mov', '.webm', '.m4v', '.ts', '.wmv', '.flv'}

@app.get('/api/create/scan-seasons/{slug}')
def create_scan_seasons(slug: str, media_root: str):
    """
    Walks a slug's folder for the "auto-fill seasons" button: every
    subfolder is treated as a season, in alphabetical order, and every
    video file directly inside it becomes an episode, also alphabetical.
    No folder-naming convention is assumed (season_1, Season 1, s1, ...
    all just sort as whatever they are) since none has been settled on.
    Doesn't touch metadata.json - just reports what's on disk so the
    frontend can build a fresh `seasons` array from it.
    """
    root_path = Path(media_root)
    if root_path not in MEDIA_ROOTS:
        raise HTTPException(status_code=400, detail=f'"{media_root}" is not a configured media root')

    slug_folder = root_path / slug
    if not slug_folder.is_dir():
        raise HTTPException(status_code=404, detail=f'Slug folder "{slug}" not found under {media_root}')

    season_folders = sorted(
        (p for p in slug_folder.iterdir() if p.is_dir()),
        key=lambda p: p.name.lower(),
    )

    seasons = []
    for folder in season_folders:
        episode_files = sorted(
            (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
            key=lambda p: p.name.lower(),
        )
        seasons.append({
            'folder_name': folder.name,
            'episode_paths': [str(p.relative_to(slug_folder)) for p in episode_files],
        })

    return {'seasons': seasons}

@app.get('/api/create/media-roots')
def create_media_roots():
    """
    Returns the configured media_dirs so the create page can offer a
    dropdown of which root to save a new (or existing) title into,
    instead of guessing.
    """
    return {'roots': [str(root) for root in MEDIA_ROOTS]}

@app.get('/api/create/browse')
def create_browse(root: str, path: str = ''):
    """
    Lists the contents of a directory under one of the configured media
    roots, for the create page's file browser. `path` is relative to
    `root` (e.g. "some_slug/season_1"), '' for the root's top level.
    Guards against escaping the root via '..' or symlinks.
    """
    root_path = Path(root)
    if root_path not in MEDIA_ROOTS:
        raise HTTPException(status_code=400, detail=f'"{root}" is not a configured media root')

    target = (root_path / path).resolve()
    try:
        target.relative_to(root_path.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail='Path escapes the media root')

    if not target.is_dir():
        raise HTTPException(status_code=404, detail='Directory not found')

    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        try:
            is_dir = item.is_dir()
            entries.append({
                'name': item.name,
                'is_dir': is_dir,
                'size': None if is_dir else item.stat().st_size,
                # Relative path from the media root, for use in the next browse call
                # or for pasting into metadata.json's episode_path/movie_path fields.
                'rel_path': str(item.relative_to(root_path)),
            })
        except OSError:
            continue  # broken symlink or permissions issue, skip it

    return {
        'root': str(root_path),
        'path': path,
        'entries': entries,
    }

@app.get('/api/create/load/{slug}')
def create_load_metadata(slug: str):
    """
    Returns the raw contents of metadata.json for a slug, for the create
    page's JSON editor. Unlike load_title(), this does NOT merge in
    tmdb_metadata.json or run it through parse_title_data — the create
    page edits the author's own metadata.json only.
    """
    root = find_title_root(slug)
    if root is None:
        raise HTTPException(status_code=404, detail='Slug not found (no metadata.json in any media root)')
    metadata_path = root / slug / 'metadata.json'
    try:
        data = json.loads(metadata_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f'Malformed JSON for {slug}: {e}')
    return {'metadata': data, 'media_root': str(root)}

@app.get('/api/create/check-asset/{slug}/{asset_path:path}')
def create_check_asset(slug: str, asset_path: str):
    """
    Reports whether a given asset path exists on disk relative to the
    slug's folder, without serving the file itself. Used by the create
    page to decide whether to point <img> tags at /media-assets/... or
    fall back to a TMDB URL.
    """
    root = find_title_root(slug)
    if root is None:
        return {'exists': False}
    full_path = root / slug / asset_path
    return {'exists': full_path.is_file()}

@app.get('/api/create/tmdb-defaults/{media_type}/{tmdb_id}')
def create_tmdb_defaults(media_type: str, tmdb_id: int):
    """
    Looks up the default poster_path/backdrop_path TMDB has on file for
    a given tmdb_id, for when metadata.json doesn't specify its own.
    Requires TMDB_READ_ACCESS_TOKEN, so this stays server-side rather
    than being called directly from the browser.
    """
    if media_type not in ('movie', 'tv'):
        raise HTTPException(status_code=400, detail="media_type must be 'movie' or 'tv'")
    if not TMDB_TOKEN:
        raise HTTPException(status_code=500, detail='TMDB_READ_ACCESS_TOKEN is not configured')
    try:
        resp = requests.get(
            f'https://api.themoviedb.org/3/{media_type}/{tmdb_id}',
            headers={'Authorization': f'Bearer {TMDB_TOKEN}'},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f'TMDB request failed: {e}')
    return {
        'poster_path': data.get('poster_path'),
        'backdrop_path': data.get('backdrop_path'),
    }

@app.post('/api/create/save/{slug}')
async def create_save_metadata(slug: str, request: Request):
    """
    Writes the JSON editor's contents to metadata.json for a slug, under
    the media root the user picked from the dropdown. Creates the slug
    folder if it doesn't already exist (new title), so this covers both
    "create new title" and "edit existing title" in one endpoint.
    """
    body = await request.json()
    media_root = request.query_params.get('media_root')
    if not media_root:
        raise HTTPException(status_code=400, detail='media_root is required (pick one from the dropdown)')

    root = Path(media_root)
    if root not in MEDIA_ROOTS:
        raise HTTPException(status_code=400, detail=f'"{media_root}" is not a configured media root')

    folder = root / slug
    folder.mkdir(parents=True, exist_ok=True)
    metadata_path = folder / 'metadata.json'
    metadata_path.write_text(json.dumps(body, indent=2), encoding='utf-8')
    return {'ok': True, 'path': str(metadata_path)}

@app.post('/api/create/generate-tmdb/{slug}')
def create_generate_tmdb(slug: str):
    """
    Fetches full TMDB metadata for a slug's tmdb_id/media_type (as
    currently saved in its metadata.json) and writes tmdb_metadata.json.
    Mirrors the startup-scan logic in __main__, callable on demand from
    the create page.
    """
    root = find_title_root(slug)
    if root is None:
        raise HTTPException(status_code=404, detail='Slug not found (no metadata.json in any media root)')
    folder = root / slug
    try:
        own_data = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f'Malformed JSON for {slug}: {e}')

    tmdb_id = own_data.get('tmdb_id')
    media_type = own_data.get('media_type')
    if tmdb_id is None or media_type not in ('movie', 'tv'):
        raise HTTPException(status_code=400, detail='metadata.json needs a tmdb_id and media_type ("movie" or "tv") first')
    if not TMDB_TOKEN:
        raise HTTPException(status_code=500, detail='TMDB_READ_ACCESS_TOKEN is not configured')

    try:
        tmdb_metadata = tmdb.fetch_metadata(tmdb_id, media_type, TMDB_TOKEN)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f'TMDB fetch failed: {e}')

    tmdb_metadata_path = folder / 'tmdb_metadata.json'
    tmdb_metadata_path.write_text(json.dumps(tmdb_metadata, indent=2), encoding='utf-8')
    return {'ok': True, 'path': str(tmdb_metadata_path)}

@app.get('/create')
async def create_page(request: Request, password: str | None = None):
    device_type = get_device_type(request.headers.get('User-Agent', ''))
    # Password is CONFIG.admin_password_hash, which is also used for WebDAV
    # Hash the provided password and compare it to the stored hash
    if password is not None:
        if not CONFIG.admin_password_hash:
            return templates.TemplateResponse(
                request=request, name='error.html',
                context={'status_code': 500, 'detail': 'Server misconfiguration: admin password hash is not set', 'error_image_path': '/www/static/img/500.png', 'device_type': device_type},
                status_code=500,
            )
        if not bcrypt.checkpw(password.encode(), CONFIG.admin_password_hash.encode()):
            # Redirect to error page with a 401 Unauthorized status code
            return templates.TemplateResponse(
                request=request, name='error.html',
                context={'status_code': 401, 'detail': 'Incorrect password', 'error_image_path': '/www/static/img/401.webp', 'device_type': device_type},
                status_code=401,
            )
        # Password is correct, set a cookie to remember it for future requests
        response = templates.TemplateResponse(
            request=request, name='create.html',
            context={'device_type': device_type},
        )
        return response

# Account stuff
'''
@app.get('/avatar/{user_id}')
def profile_picture(user_id: str, request: Request):
    path = os.path.join('dat', int_to_base64(user_id), 'avatar.png')
    if not path.exists():
        # fall back to the default error/placeholder image
        path = WWW_ROOT / 'static' / 'img' / 'error.png'
    return FileResponse(path)
'''

# Error pages
@app.exception_handler(404)
def not_found(request: Request, exc: HTTPException):
    device_type = get_device_type(request.headers.get('User-Agent', ''))
    return templates.TemplateResponse(
        request=request, name='error.html',
        context={'status_code': 404, 'detail': 'Page not found', 'error_image_path': '/www/static/img/404.jpg', 'device_type': device_type},
        status_code=404,
    )
@app.exception_handler(400)
def bad_request(request: Request, exc: HTTPException):
    device_type = get_device_type(request.headers.get('User-Agent', ''))
    return templates.TemplateResponse(
        request=request, name='error.html',
        context={'status_code': 400, 'detail': 'Bad request', 'error_image_path': '/www/static/img/400.jpg', 'device_type': device_type},
        status_code=400,
    )
@app.exception_handler(500)
def internal_error(request: Request, exc: HTTPException):
    device_type = get_device_type(request.headers.get('User-Agent', ''))
    return templates.TemplateResponse(
        request=request, name='error.html',
        context={'status_code': 500, 'detail': 'Internal server error', 'error_image_path': '/www/static/img/500.png', 'device_type': device_type},
        status_code=500,
    )

if __name__ == '__main__':
    port = CONFIG.port
    log_level = CONFIG.log_level
    cfg_require_token = CONFIG.require_token
    MEDIA_ROOTS = [Path(d) for d in CONFIG.media_dirs]

    # Create accounts
    if CONFIG.accounts:
        if not accounts.accounts_db_exists():
            if accounts.create_accounts_db():
                log(type='info', message='Created accounts database')
            else:
                log(type='error', message='Failed to create accounts database')

    # Get all titles and put them into _title_cache for quick access by slug
    titles = scan_library(sort=False)
    _title_cache = {t['_slug']: t for t in titles}
    print(_title_cache)

    answer = input('Scan library for new titles? (y/n): ').strip().lower()
    if answer == 'y':
        # Get movie info from TMDb at startup for any titles that have a tmdb_id but no tmdb_metadata.json yet
        scanned_tmdb_movies_count = 0
        for title in titles:
            if title['tmdb_id'] is not None:
                _root = find_title_root(title['_slug'])
                if _root is None:
                    continue
                folder = _root / title['_slug']
                tmdb_metadata_path = folder / 'tmdb_metadata.json'
                if not tmdb_metadata_path.exists():
                    try:
                        if scanned_tmdb_movies_count == 0:
                            print('Fetching TMDb data for media with missing tmdb_metadata.json...')
                        tmdb_metadata = tmdb.fetch_metadata(title['tmdb_id'], title['media_type'], TMDB_TOKEN)
                        tmdb_metadata_path.write_text(json.dumps(tmdb_metadata, indent=2), encoding='utf-8')
                        scanned_tmdb_movies_count += 1
                        print(f'Fetched TMDb data for {title["tmdb_id"]} ({title["media_type"]})')
                        time.sleep(0.1)  # Avoid hitting TMDb rate limits
                    except Exception as e:
                        print(f'Failed to fetch TMDb data for {title["tmdb_id"]}: {e}')

    answer = input('Probe all video files for duration? (y/n): ').strip().lower()
    if answer == 'y':
        # Cache the duration of all video files
        paths_to_probe = []
        for title in titles:
            _root = find_title_root(title['_slug'])
            if _root is None:
                continue
            folder = _root / title['_slug']
            if title['media_type'] == 'movie':
                movie_path = folder / title['movie_path']
                if movie_path.exists() and get_cached_duration(movie_path) is None:
                    paths_to_probe.append(movie_path)
            elif title['media_type'] == 'tv':
                for season in title.get('seasons', []):
                    for episode in season.get('episodes', []):
                        if episode.get('episode_path'):
                            episode_path = folder / episode['episode_path']
                            if episode_path.exists() and get_cached_duration(episode_path) is None:
                                paths_to_probe.append(episode_path)
        # Probe all uncached paths in parallel
        if paths_to_probe:
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(get_cached_duration, path) for path in paths_to_probe]
                for _ in as_completed(futures):
                    pass  # Results are cached internally by get_cached_duration

    answer = input('Generate scrubbers for all video files? (y/n): ').strip().lower()
    if answer == 'y':
        # For every movie_path and episode_path, generate a scrubber that exists in
        # ./cache/<path_of_video_file_relative_to_its_media_root>/scrubber_<video_file_name>.json.
        # This is used to show the thumbnail preview when scrubbing the video.
        for title in titles:
            _root = find_title_root(title['_slug'])
            if _root is None:
                continue
            folder = _root / title['_slug']
            if title['media_type'] == 'movie':
                video_path = folder / title['movie_path']
                if video_path.exists():
                    cache_dir = get_cache_dir(folder, video_path)
                    scrubber_path = cache_dir / f'scrubber_{video_path.name}.json'
                    if not scrubber_path.exists():
                        try:
                            scrubber_data = generate_sprite_sheet(video_path, cache_dir / f'scrubber_{video_path.name}.jpg')
                            scrubber_path.write_text(json.dumps(scrubber_data, indent=2), encoding='utf-8')
                            print(f'Generated scrubber for {video_path}')
                        except Exception as e:
                            print(f'Failed to generate scrubber for {video_path}: {e}')
            elif title['media_type'] == 'tv':
                for season in title.get('seasons', []):
                    for episode in season.get('episodes', []):
                        if episode.get('episode_path'):
                            video_path = folder / episode['episode_path']
                            if video_path.exists():
                                cache_dir = get_cache_dir(folder, video_path)
                                scrubber_path = cache_dir / f'scrubber_{video_path.name}.json'
                                if not scrubber_path.exists():
                                    try:
                                        scrubber_data = generate_sprite_sheet(video_path, cache_dir / f'scrubber_{video_path.name}.jpg')
                                        scrubber_path.write_text(json.dumps(scrubber_data, indent=2), encoding='utf-8')
                                        print(f'Generated scrubber for {video_path}')
                                    except Exception as e:
                                        print(f'Failed to generate scrubber for {video_path}: {e}')

    # Probe once at startup for available transcoders
    transcoders = transcode.probe_transcoders()

    if CONFIG.host_webdav:
        import network_storage
        webdav_thread = threading.Thread(
            target=lambda: HTTPServer(('127.0.0.1', CONFIG.webdav_port), network_storage.WebDAVHandler).serve_forever(),
            daemon=True,  # dies automatically when the main process exits
        )
        webdav_thread.start()

    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=port, log_level=log_level)
