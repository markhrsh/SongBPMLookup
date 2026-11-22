"""Title/artist normalization and fuzzy matching between Discogs and Deezer."""

import re
import unicodedata

from rapidfuzz import fuzz

ALBUM_MIN_SCORE = 80
TRACK_MIN_SCORE = 85
ARTIST_MIN_SCORE = 80
DURATION_TOLERANCE = 15   # seconds

_VERSION_WORDS = (r"remaster(?:ed)?|deluxe|edition|version|expanded|anniversary|bonus|"
                  r"mono|stereo|single|edit")
_BRACKETED = re.compile(r"[\(\[][^\)\]]*\b(?:" + _VERSION_WORDS + r")\b[^\)\]]*[\)\]]", re.IGNORECASE)
_DASH_SUFFIX = re.compile(r"\s+[-–—]\s+[^-–—]*\b(?:" + _VERSION_WORDS + r")\b.*$", re.IGNORECASE)


def strip_version(text):
    """'Song - 2009 Remaster' / 'Album (Deluxe Edition) [2009 Remaster]' -> base title."""
    text = _BRACKETED.sub(" ", text or "")
    text = _DASH_SUFFIX.sub("", text)
    return text.strip()


def normalize(text):
    text = strip_version(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"\bpt\b\.?", "part", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_score(a, b):
    return fuzz.token_sort_ratio(normalize(a), normalize(b))


def artist_score(a, b):
    # token_set tolerates "feat." credits and "X & Y" vs "X"
    return fuzz.token_set_ratio(normalize(a), normalize(b))


def is_various(artist):
    return normalize(artist) in {"various", "various artists"}


def album_score(discogs_artist, discogs_album, dz_artist, dz_album):
    # token_sort (not token_set): "Head Hunters" must not score 100 against "Head Hunters Live"
    alb = fuzz.token_sort_ratio(normalize(discogs_album), normalize(dz_album))
    if is_various(discogs_artist):
        return alb
    return min(alb, artist_score(discogs_artist, dz_artist))


def best_album(discogs_artist, discogs_album, results):
    """Return (deezer_album, score) for the best-scoring result >= ALBUM_MIN_SCORE, else (None, best)."""
    best, best_s = None, 0
    for r in results:
        s = album_score(discogs_artist, discogs_album,
                        (r.get("artist") or {}).get("name", ""), r.get("title", ""))
        # prefer more tracks on ties (fuller editions usually contain every Discogs track)
        if s > best_s or (s == best_s and best and r.get("nb_tracks", 0) > best.get("nb_tracks", 0)):
            best, best_s = r, s
    return (best, best_s) if best_s >= ALBUM_MIN_SCORE else (None, best_s)


def _duration_key(d_secs, dz_secs):
    if not d_secs or not dz_secs:
        return (0, 0)            # unknown: neutral
    diff = abs(d_secs - dz_secs)
    return (1 if diff <= DURATION_TOLERANCE else -1, -diff)


def match_tracks(discogs_tracks, deezer_tracks):
    """Greedy one-to-one assignment. Returns {discogs_index: (deezer_track, score)}."""
    pairs = []
    for i, t in enumerate(discogs_tracks):
        for j, dz in enumerate(deezer_tracks):
            s = title_score(t["title"], dz.get("title", ""))
            if s >= TRACK_MIN_SCORE:
                pairs.append((s, _duration_key(t["duration"], dz.get("duration")), i, j))
    pairs.sort(key=lambda p: (p[0], p[1]), reverse=True)
    used_i, used_j, out = set(), set(), {}
    for s, _, i, j in pairs:
        if i in used_i or j in used_j:
            continue
        used_i.add(i); used_j.add(j)
        out[i] = (deezer_tracks[j], s)
    return out


def best_track_search(track, artist, results, exclude_ids=()):
    """Pick the best /search result for one Discogs track. Returns (deezer_track, score) or (None, 0)."""
    cands = []
    for dz in results:
        if dz.get("id") in exclude_ids:
            continue
        s = title_score(track["title"], dz.get("title", ""))
        a = artist_score(artist, (dz.get("artist") or {}).get("name", ""))
        if s >= TRACK_MIN_SCORE and a >= ARTIST_MIN_SCORE:
            cands.append((s, _duration_key(track["duration"], dz.get("duration")), dz))
    if not cands:
        return None, 0
    s, _, dz = max(cands, key=lambda c: (c[0], c[1]))
    return dz, s
