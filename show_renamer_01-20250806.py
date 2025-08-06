#!/usr/bin/env python3
"""
show_renamer.py: Rename TV show and movie files to Plex naming conventions.
Requires:
  - Python 3.9+
  - requests (`pip3 install requests`)
  - rapidfuzz (`pip3 install rapidfuzz`)
Set your TMDb API key in the environment:
  export TMDB_API_KEY="your_api_key_here"

Usage:
  show_renamer.py --type tv --dir <DIR> [--dry-run]
  show_renamer.py --type movie --dir <DIR> [--dry-run]
"""

import os
import sys
import re
import argparse
import logging
import requests
from rapidfuzz import fuzz, process

# —— CONFIG —————————————————————————————————————————————————————————————
VIDEO_EXTS        = {'.mkv', '.mp4', '.avi', '.mov', '.wmv'}
STOPWORDS         = {"the", "and", "of", "a", "to", "in", "on", "at", "for"}
FUZZY_THRESHOLD   = 80   # % for show matching
EPISODE_THRESHOLD = 70   # % for episode-title matching
# ———————————————————————————————————————————————————————————————————————

def clean_keywords(text: str):
    """Split text into lowercase words, removing stopwords."""
    return [w for w in re.split(r'\W+', text.lower()) if w and w not in STOPWORDS]

def fuzzy_match(query: str, candidates: list, threshold: int = FUZZY_THRESHOLD):
    """Return best fuzzy match ≥ threshold, else None."""
    if not candidates:
        return None
    match, score, _ = process.extractOne(query, candidates, scorer=fuzz.token_sort_ratio)
    return match if score >= threshold else None

def split_show_and_rest(filename_base: str) -> str:
    """
    Given something like:
      Dexters.Laboratory.S03E12.Jeepers.Creepers…
    return just:
      Dexters.Laboratory
    """
    # 1) cut at the SxxExx or xXxx/E12 marker
    m = re.match(r'^(.*?)(?:[._\s][sS]?\d{1,2}[xXeE]\d{1,2})', filename_base)
    if m:
        return m.group(1)
    # 2) else cut at quality/release tags
    parts = re.split(r'[._\s](?:\d{3,4}p|WEB[-_.]DL|HDTV|DD\d\.\d|BluRay)', filename_base)
    return parts[0]

def fuzzy_match_show(show_name: str, all_shows: list):
    # exact match first
    for s in all_shows:
        title = s.get('name') or s.get('title') or ''
        if title.lower() == show_name.lower():
            return s
    # fuzzy match on cleaned keywords
    query = " ".join(clean_keywords(show_name))
    titles = [s.get('name') or s.get('title') for s in all_shows]
    best = fuzzy_match(query, titles)
    if best:
        return next(s for s in all_shows if (s.get('name') or s.get('title')) == best)
    return None

def extract_season_episode(name: str):
    """Try to parse SxxExx, xXxx, Exx, or 3-digit patterns."""
    patterns = [
        (r"[sS](\d{1,2})[eE](\d{1,2})", lambda m: (int(m.group(1)), int(m.group(2)))),
        (r"(\d{1,2})[xX](\d{1,2})",     lambda m: (int(m.group(1)), int(m.group(2)))),
        (r"[eE](\d{1,2})",             lambda m: (None, int(m.group(1)))),
        (r"\b(\d{3})\b",               lambda m: (int(m.group(1)[0]), int(m.group(1)[1:])))
    ]
    for patt, fn in patterns:
        m = re.search(patt, name)
        if m:
            return fn(m)
    return None, None

def fuzzy_match_episode(hint: str, episodes: list):
    # try explicit season/episode
    season, num = extract_season_episode(hint)
    if season is not None and num is not None:
        for ep in episodes:
            sn = ep.get('season_number') or ep.get('season')
            en = ep.get('episode_number') or ep.get('number')
            if sn == season and en == num:
                return ep
    # fuzzy on title
    query = " ".join(clean_keywords(hint))
    titles = [ep.get('name') or ep.get('title') for ep in episodes]
    best = fuzzy_match(query, titles, threshold=EPISODE_THRESHOLD)
    if best:
        return next(ep for ep in episodes if (ep.get('name') or ep.get('title')) == best)
    return None

def fetch_tv_shows(api_key: str, query: str):
    url = "https://api.themoviedb.org/3/search/tv"
    r = requests.get(url, params={'api_key': api_key, 'query': query})
    r.raise_for_status()
    return r.json().get('results', [])

def fetch_tv_details(api_key: str, tv_id: int):
    url = f"https://api.themoviedb.org/3/tv/{tv_id}"
    r = requests.get(url, params={'api_key': api_key})
    r.raise_for_status()
    return r.json()

def fetch_tv_episodes(api_key: str, tv_id: int, season: int):
    url = f"https://api.themoviedb.org/3/tv/{tv_id}/season/{season}"
    r = requests.get(url, params={'api_key': api_key})
    r.raise_for_status()
    return r.json().get('episodes', [])

def fetch_movie_results(api_key: str, query: str):
    url = "https://api.themoviedb.org/3/search/movie"
    r = requests.get(url, params={'api_key': api_key, 'query': query})
    r.raise_for_status()
    return r.json().get('results', [])

def rename_tv_file(path: str, dry_run: bool, api_key: str, rename_logger, error_logger):
    dirname, fname = os.path.split(path)
    base, ext = os.path.splitext(fname)

    # split into show hint + full episode hint
    show_hint     = split_show_and_rest(base)
    episode_hint  = base
    clean_query   = " ".join(clean_keywords(show_hint))

    # 1) find show via TMDb search on cleaned query
    shows = fetch_tv_shows(api_key, clean_query)
    show  = fuzzy_match_show(show_hint, shows)
    if not show:
        error_logger.warning(f"Show not found: {fname}")
        return

    tv_id = show['id']
    title = show.get('name') or show.get('original_name')

    # 2) fetch seasons & episodes
    details = fetch_tv_details(api_key, tv_id)
    seasons = details.get('seasons', [])
    all_eps = []
    for s in seasons:
        num = s.get('season_number')
        try:
            eps = fetch_tv_episodes(api_key, tv_id, num)
        except Exception:
            continue
        for ep in eps:
            ep['season_number'] = num
            all_eps.append(ep)

    # 3) find episode
    ep = fuzzy_match_episode(episode_hint, all_eps)
    if not ep:
        error_logger.warning(f"Episode not found: {fname}")
        return

    s = ep['season_number']
    e = ep.get('episode_number') or ep.get('number')
    ep_title = ep.get('name') or ep.get('title')

    new_name = f"{title} - S{s:02d}E{e:02d} - {ep_title}{ext}"
    old = path
    new = os.path.join(dirname, new_name)
    if old == new:
        return

    rename_logger.info(f"{old} -> {new}")
    if not dry_run:
        os.rename(old, new)

def rename_movie_file(path: str, dry_run: bool, api_key: str, rename_logger, error_logger):
    dirname, fname = os.path.split(path)
    base, ext = os.path.splitext(fname)

    results = fetch_movie_results(api_key, base)
    movie = next((m for m in results if m.get('title','').lower() == base.lower()), None)
    if not movie:
        query  = " ".join(clean_keywords(base))
        titles = [m.get('title') for m in results]
        best   = fuzzy_match(query, titles)
        if best:
            movie = next(m for m in results if m.get('title') == best)
    if not movie:
        error_logger.warning(f"Movie not found: {fname}")
        return

    title = movie.get('title')
    year  = (movie.get('release_date') or '')[:4]
    new_name = f"{title} ({year}){ext}"
    old = path
    new = os.path.join(dirname, new_name)
    if old == new:
        return

    rename_logger.info(f"{old} -> {new}")
    if not dry_run:
        os.rename(old, new)

def main():
    p = argparse.ArgumentParser(description="Rename TV shows or movies in place.")
    p.add_argument('--type', choices=['tv','movie'], required=True, help="tv or movie")
    p.add_argument('--dir',  required=True, help="Directory to process")
    p.add_argument('--dry-run', action='store_true', help="Show changes without renaming")
    args = p.parse_args()

    api_key = os.getenv('TMDB_API_KEY')
    if not api_key:
        print("Error: TMDB_API_KEY not set in environment.", file=sys.stderr)
        sys.exit(1)

    # setup logging
    rename_logger = logging.getLogger('rename')
    rename_logger.setLevel(logging.INFO)
    rename_h = logging.FileHandler('rename_log.txt')
    rename_logger.addHandler(rename_h)

    error_logger = logging.getLogger('error')
    error_logger.setLevel(logging.WARNING)
    error_h = logging.FileHandler('error_log.txt')
    error_logger.addHandler(error_h)

    for root, _, files in os.walk(args.dir):
        for f in files:
            if os.path.splitext(f)[1].lower() not in VIDEO_EXTS:
                continue
            full = os.path.join(root, f)
            try:
                if args.type == 'tv':
                    rename_tv_file(full, args.dry_run, api_key, rename_logger, error_logger)
                else:
                    rename_movie_file(full, args.dry_run, api_key, rename_logger, error_logger)
            except Exception as ex:
                error_logger.warning(f"Error processing {full}: {ex}")

if __name__ == "__main__":
    main()