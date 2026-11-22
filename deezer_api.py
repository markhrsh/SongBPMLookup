"""Minimal Deezer public API client: rate limiting, retries and a JSON cache."""

import hashlib
import json
import sys
import time
from collections import Counter, deque

import requests

import config

BASE_URL = "https://api.deezer.com"
CACHE_ROOT = config.CACHE_DIR.parent / "deezer"


class DeezerError(Exception):
    pass


class DeezerClient:
    WINDOW = 5.0          # seconds
    MAX_IN_WINDOW = 45    # Deezer allows 50 per 5s; keep a margin
    MAX_RETRIES = 6
    BACKOFF = [2, 4, 8, 16, 30, 60]

    def __init__(self, refresh=False):
        self.refresh = refresh
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self._sent = deque()
        self.calls_by_kind = Counter()

    # -- low level -----------------------------------------------------------
    def _throttle(self):
        now = time.monotonic()
        while self._sent and now - self._sent[0] > self.WINDOW:
            self._sent.popleft()
        if len(self._sent) >= self.MAX_IN_WINDOW:
            time.sleep(self.WINDOW - (now - self._sent[0]) + 0.05)
        self._sent.append(time.monotonic())

    def _fetch(self, url, params):
        for attempt in range(self.MAX_RETRIES):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                delay = self.BACKOFF[attempt]
                print(f"  Deezer network error ({type(exc).__name__}); retrying in {delay}s",
                      file=sys.stderr)
                time.sleep(delay)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                delay = self.BACKOFF[attempt]
                print(f"  Deezer HTTP {resp.status_code}; backing off {delay}s", file=sys.stderr)
                time.sleep(delay)
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                raise DeezerError(f"HTTP {resp.status_code} for {url}")
            data = resp.json()
            err = data.get("error") if isinstance(data, dict) else None
            if err:
                code = err.get("code")
                if code == 4:                     # quota exceeded: back off and retry
                    delay = self.BACKOFF[attempt]
                    print(f"  Deezer quota limit; backing off {delay}s", file=sys.stderr)
                    time.sleep(delay)
                    continue
                if code == 800:                   # "no data": treat as not found
                    return None
                raise DeezerError(f"Deezer error {code}: {err.get('message', '')}")
            return data
        raise DeezerError(f"Giving up on {url} after {self.MAX_RETRIES} attempts")

    def get(self, kind, path_or_url, params=None):
        """Cached GET. `kind` names the cache subfolder (search_album, track, ...)."""
        url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
        key_src = url + "?" + json.dumps(params or {}, sort_keys=True)
        cache_file = CACHE_ROOT / kind / (hashlib.sha1(key_src.encode()).hexdigest() + ".json")
        if cache_file.exists() and not self.refresh:
            with cache_file.open(encoding="utf-8") as f:
                data = json.load(f)
            return None if data == {"__not_found__": True} else data
        self.calls_by_kind[kind] += 1
        data = self._fetch(url, params)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data if data is not None else {"__not_found__": True}, f, ensure_ascii=False)
        tmp.replace(cache_file)
        return data

    # -- endpoints -----------------------------------------------------------
    def search_album(self, query):
        return (self.get("search_album", "/search/album", {"q": query, "limit": 25}) or {}).get("data", [])

    def album_tracks(self, album_id):
        tracks, url, params = [], f"/album/{album_id}/tracks", {"limit": 100}
        while url:
            data = self.get("album_tracks", url, params) or {}
            tracks += data.get("data", [])
            url, params = data.get("next"), None
        return tracks

    def search_track(self, query):
        return (self.get("search_track", "/search", {"q": query, "limit": 25}) or {}).get("data", [])

    def track(self, track_id):
        return self.get("track", f"/track/{track_id}")
