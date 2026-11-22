"""Minimal Discogs API client with rate limiting, retries and a release cache."""

import json
from collections import Counter
import os
import sys
import time

import requests
from dotenv import load_dotenv

import config


class DiscogsError(Exception):
    pass


class DiscogsClient:
    MIN_INTERVAL = 1.0          # seconds between real requests (60/min)
    MAX_RETRIES = 6
    BACKOFF = [10, 20, 40, 60, 60, 60]

    def __init__(self):
        load_dotenv(config.PROJECT_DIR / ".env")
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            sys.exit("DISCOGS_TOKEN is not set. Add it to the .env file.")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Discogs token={token}",
            "User-Agent": config.USER_AGENT,
        })
        self._last_request = 0.0
        self.api_calls = 0
        self.calls_by_kind = Counter()

    # -- low level -----------------------------------------------------------
    def _throttle(self):
        wait = self.MIN_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _react_to_remaining(self, resp):
        try:
            remaining = int(resp.headers.get("X-Discogs-Ratelimit-Remaining", "60"))
        except ValueError:
            return
        if remaining <= 3:
            time.sleep(15)
        elif remaining <= 10:
            time.sleep(3)

    @staticmethod
    def _kind(path):
        if path.endswith("/collection/folders/0/releases"):
            return "collection pages"
        if path.endswith("/collection/folders"):
            return "folders lookup"
        if path.endswith("/collection/fields"):
            return "fields lookup"
        if path.startswith("/releases/"):
            return "release details"
        if path.startswith("/masters/"):
            return "master details"
        if "/collection/releases/" in path:
            return "collection instances"
        return "other"

    def get(self, path, params=None):
        """GET a JSON resource. Returns None on 404."""
        url = config.BASE_URL + path
        for attempt in range(self.MAX_RETRIES):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                self._last_request = time.monotonic()
                delay = self.BACKOFF[attempt]
                print(f"\n  network error ({type(exc).__name__}); retrying in {delay}s",
                      file=sys.stderr)
                time.sleep(delay)
                continue
            self._last_request = time.monotonic()
            self.api_calls += 1
            self.calls_by_kind[self._kind(path)] += 1

            if resp.status_code == 200:
                self._react_to_remaining(resp)
                return resp.json()
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() \
                    else self.BACKOFF[attempt]
                print(f"\n  HTTP {resp.status_code}; backing off {delay}s", file=sys.stderr)
                time.sleep(delay)
                continue
            if resp.status_code == 401:
                raise DiscogsError("HTTP 401 Unauthorized: check DISCOGS_TOKEN in .env")
            raise DiscogsError(f"HTTP {resp.status_code} for {path}")
        raise DiscogsError(f"Giving up on {path} after {self.MAX_RETRIES} attempts")

    # -- collection ----------------------------------------------------------
    def get_collection(self):
        path = f"/users/{config.USERNAME}/collection/folders/0/releases"
        items, page, pages = [], 1, 1
        while page <= pages:
            data = self.get(path, params={"per_page": 100, "page": page})
            if data is None:
                raise DiscogsError(f"Collection for {config.USERNAME} not found")
            pages = data["pagination"]["pages"]
            print(f"\rCollection page {page}/{pages}", end="", flush=True)
            for rel in data["releases"]:
                bi = rel.get("basic_information", {})
                items.append({
                    "id": bi.get("id", rel.get("id")),
                    "title": bi.get("title", ""),
                    "artists": bi.get("artists", []),
                    "year": bi.get("year"),
                    "labels": bi.get("labels", []),
                    "formats": bi.get("formats", []),
                    "genres": bi.get("genres", []) or [],
                    "styles": bi.get("styles", []) or [],
                    "date_added": rel.get("date_added"),
                    "folder_id": rel.get("folder_id"),
                    "instance_id": rel.get("instance_id"),
                    "notes": rel.get("notes", []) or [],
                })
            page += 1
        print()
        return items

    def get_folders(self, cached=False, refresh=False):
        """Return {folder_id: folder_name}."""
        path = f"/users/{config.USERNAME}/collection/folders"
        if cached:
            data, _ = self.get_cached(path, "folders", config.USERNAME, refresh)
        else:
            data = self.get(path)
        return {f["id"]: f["name"] for f in (data or {}).get("folders", [])}

    def get_fields(self):
        """Return the custom collection fields in display order: [{id, name, type}]."""
        data = self.get(f"/users/{config.USERNAME}/collection/fields") or {}
        fields = sorted(data.get("fields", []), key=lambda f: f.get("position", 0))
        return [{"id": f["id"], "name": f["name"], "type": f.get("type")} for f in fields]

    # -- cached lookups -----------------------------------------------------
    NOT_FOUND = {"__not_found__": True}

    def get_cached(self, path, subdir, key, refresh=False, cache_404=False):
        """GET with a JSON cache at cache/<subdir>/<key>.json.

        Returns (data_or_None, from_cache). With cache_404, a 404 is remembered so
        reruns don't ask again (cleared by refresh)."""
        cache_dir = config.CACHE_DIR.parent / subdir
        cache_file = cache_dir / f"{key}.json"
        if cache_file.exists() and not refresh:
            with cache_file.open(encoding="utf-8") as f:
                data = json.load(f)
            return (None if data == self.NOT_FOUND else data), True
        data = self.get(path)
        if data is not None or cache_404:
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data if data is not None else self.NOT_FOUND, f, ensure_ascii=False)
            tmp.replace(cache_file)
        return data, False

    def get_release(self, release_id, refresh=False):
        """Returns (release_json_or_None, from_cache: bool). Cached in cache/releases/."""
        return self.get_cached(f"/releases/{release_id}", config.CACHE_DIR.name,
                               release_id, refresh)

    def get_master(self, master_id, refresh=False):
        return self.get_cached(f"/masters/{master_id}", "masters", master_id, refresh,
                               cache_404=True)

    def get_collection_instances(self, release_id, refresh=False):
        """Your copies of a release: list of {instance_id, folder_id, ...}; [] if not owned."""
        data, cached = self.get_cached(
            f"/users/{config.USERNAME}/collection/releases/{release_id}",
            "collection_instances", release_id, refresh, cache_404=True)
        return (data or {}).get("releases", []), cached
