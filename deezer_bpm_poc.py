"""Proof of concept: per-track BPM from Deezer for Discogs releases listed in a Google Sheet.

Reads release IDs from the "Albums" tab, builds each track list from Discogs, looks up
BPM on Deezer and writes one row per track to the "Tracks" tab.

    python deezer_bpm_poc.py --dry-run --limit 2   # print matches, write nothing
    python deezer_bpm_poc.py                       # full run, writes to the sheet
    python deezer_bpm_poc.py --dry-run --ids 123,456   # test without the sheet
"""

import argparse
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

import config
import matching
from deezer_api import DeezerClient, DeezerError
from discogs_api import DiscogsClient, DiscogsError

ALBUMS_TAB, TRACKS_TAB = "Albums", "Tracks"
ALBUM_COLS = {"id": "Discogs Release ID", "artist": "Artist", "album": "Album",
              "notes": "Personal Notes", "status": "Status"}
TRACK_HEADER = ["Artist", "Album", "Genre", "Style", "Folder", "Year", "Position",
                "Track Name", "BPM", "Flag", "Deezer Track", "Personal Notes", "Credits"]
TEMPO_LOW, TEMPO_HIGH = 70, 160


# --- Discogs helpers ---------------------------------------------------------
def clean_artist_name(name):
    name = re.sub(r"\s+\(\d+\)$", "", (name or "").strip())
    return name.rstrip("*").strip()


def join_artists(artists):
    """Discogs artist list -> 'A & B', using each artist's join string."""
    out = ""
    for i, a in enumerate(artists or []):
        out += clean_artist_name(a.get("anv") or a.get("name"))
        join = (a.get("join") or "").strip()
        if i < len(artists) - 1:
            out += f" {join} " if join and join != "," else ", "
    return re.sub(r"\s+", " ", out).strip()


def parse_duration(text):
    parts = [p for p in (text or "").strip().split(":") if p.isdigit()]
    if not parts:
        return None
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return secs or None


def _positions_covered(spec, order):
    """'A1 to A3, B2' -> {'A1','A2','A3','B2'} using the release's track order."""
    idx = {p.lower(): i for i, p in enumerate(order)}
    out = set()
    for part in re.split(r"[,&]", spec or ""):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(.+?)\s+to\s+(.+)", part, re.IGNORECASE)
        if m and m.group(1).lower() in idx and m.group(2).lower() in idx:
            a, b = sorted((idx[m.group(1).lower()], idx[m.group(2).lower()]))
            out.update(p.lower() for p in order[a:b + 1])
        else:
            out.add(part.lower())
    return out


def format_credits(credits):
    """[(name, role)] -> 'Name (Role, Role); Name (Role)', grouped by person, in order."""
    by_person = {}
    for name, role in credits:
        roles = by_person.setdefault(name, [])
        for r in [x.strip() for x in re.split(r",(?![^\[]*\])", role or "") if x.strip()]:
            if r not in roles:
                roles.append(r)
    return "; ".join(f"{n} ({', '.join(r)})" if r else n for n, r in by_person.items())


def discogs_tracks(release, album_artist):
    out = []
    # release-level credits that name specific tracks ("tracks": "A1 to A3, B2");
    # album-wide credits (no "tracks") are left out so they don't repeat on every row
    order = []
    for t in release.get("tracklist", []):
        if t.get("type_", "track") == "track":
            order.append(t.get("position") or "")
        elif t.get("type_") == "index":
            order += [st.get("position") or "" for st in t.get("sub_tracks", []) or []]
    scoped = [(ea, _positions_covered(ea.get("tracks"), order))
              for ea in release.get("extraartists", []) or [] if (ea.get("tracks") or "").strip()]

    def add(t):
        pos = t.get("position") or ""
        credits = [(clean_artist_name(ea.get("name") or ea.get("anv")), ea.get("role"))
                   for ea in t.get("extraartists", []) or []]
        credits += [(clean_artist_name(ea.get("name") or ea.get("anv")), ea.get("role"))
                    for ea, covered in scoped if pos.lower() in covered]
        out.append({
            "position": pos,
            "title": t.get("title") or "",
            "duration": parse_duration(t.get("duration")),
            "artist": join_artists(t["artists"]) if t.get("artists") else album_artist,
            "credits": format_credits(credits),
        })

    for t in release.get("tracklist", []):
        kind = t.get("type_", "track")
        if kind == "track":
            add(t)
        elif kind == "index":
            for st in t.get("sub_tracks", []) or []:
                if st.get("type_", "track") == "track":
                    add(st)
    return out


def fmt_dur(secs):
    return f"{secs // 60}:{secs % 60:02d}" if secs else "?"


# --- per-album pipeline ------------------------------------------------------
def process_album(release_id, discogs, deezer, folder_names, refresh):
    release, _ = discogs.get_release(release_id, refresh)
    if not release:
        raise ValueError(f"Discogs release {release_id} not found")
    artist = join_artists(release.get("artists"))
    title = release.get("title", "")
    year = release.get("year") or None
    if release.get("master_id"):
        master, _ = discogs.get_master(release["master_id"], refresh)
        if master and master.get("year"):
            year = master["year"]
    instances, _ = discogs.get_collection_instances(release_id, refresh)
    folders = list(dict.fromkeys(folder_names.get(i.get("folder_id"), f"Folder {i.get('folder_id')}")
                                 for i in instances))
    tracks = discogs_tracks(release, artist)

    # 1. album search
    dz_album, album_score = None, 0
    for q in (f'artist:"{artist}" album:"{matching.strip_version(title)}"',
              f"{artist} {matching.strip_version(title)}"):
        results = deezer.search_album(q)
        if results:
            dz_album, album_score = matching.best_album(artist, title, results)
            if dz_album:
                break

    # 2-3. album tracks + one-to-one title matching
    matched = {}
    if dz_album:
        dz_tracks = deezer.album_tracks(dz_album["id"])
        matched = {i: (dz, s, "album") for i, (dz, s) in matching.match_tracks(tracks, dz_tracks).items()}

    # 4. fallback track search
    used = {dz["id"] for dz, _, _ in matched.values()}
    for i, t in enumerate(tracks):
        if i in matched:
            continue
        base = matching.strip_version(t["title"])
        dz, s = None, 0
        # Deezer's advanced syntax often returns nothing, so fall back to a plain query
        for q in (f'artist:"{t["artist"]}" track:"{base}"', f'{t["artist"]} {base}'):
            dz, s = matching.best_track_search(t, t["artist"], deezer.search_track(q), used)
            if dz:
                break
        if dz:
            matched[i] = (dz, s, "track search")
            used.add(dz["id"])

    # 5. BPM (with fallback to another Deezer release of the same recording)
    for i, t in enumerate(tracks):
        t.update(deezer=None, score=None, method=None, bpm=None, bpm_from=None)
        if i in matched:
            dz, s, method = matched[i]
            t.update(deezer=dz, score=round(s), method=method)
            t["bpm"] = track_bpm(deezer, dz["id"])
            if t["bpm"] is None:
                alt, bpm = bpm_from_other_release(deezer, t, dz)
                if alt:
                    t["bpm"], t["bpm_from"] = bpm, alt

    return {
        "artist": artist, "title": title, "year": year or "",
        "genres": ", ".join(release.get("genres") or []),
        "styles": ", ".join(release.get("styles") or []),
        "folder": ", ".join(folders) if folders else "Not in collection",
        "tracks": tracks, "dz_album": dz_album, "album_score": round(album_score),
    }


def track_bpm(deezer, track_id):
    bpm = (deezer.track(track_id) or {}).get("bpm")
    return round(float(bpm), 1) if bpm else None


BPM_FALLBACK_MAX_LOOKUPS = 5   # /track calls per track at most


def bpm_from_other_release(deezer, t, matched_dz):
    """Find the same recording on another Deezer release that has a BPM.

    Same rules as matching (title >= 85, artist >= 80) plus a duration check: within
    15s of the Discogs duration or of the already-matched Deezer track (same recording,
    even when Deezer's edit differs from your pressing).
    Returns (deezer_track, bpm) or (None, None)."""
    refs = [d for d in (t["duration"], matched_dz.get("duration")) if d]
    if not refs:
        return None, None                       # no duration to confirm it's the same recording
    base = matching.strip_version(t["title"])
    seen, cands = {matched_dz["id"]}, []
    for q in (f'artist:"{t["artist"]}" track:"{base}"', f'{t["artist"]} {base}'):
        for dz in deezer.search_track(q):
            if dz.get("id") in seen or not dz.get("duration"):
                continue
            seen.add(dz["id"])
            s = matching.title_score(t["title"], dz.get("title", ""))
            a = matching.artist_score(t["artist"], (dz.get("artist") or {}).get("name", ""))
            diff = min(abs(dz["duration"] - r) for r in refs)
            if s >= matching.TRACK_MIN_SCORE and a >= matching.ARTIST_MIN_SCORE \
                    and diff <= matching.DURATION_TOLERANCE:
                cands.append((s, -diff, dz))
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)
    for _, _, dz in cands[:BPM_FALLBACK_MAX_LOOKUPS]:
        bpm = track_bpm(deezer, dz["id"])
        if bpm is not None:
            return dz, bpm
    return None, None


def track_flags(t):
    flags = []
    if not t["deezer"]:
        flags.append("No Deezer match")
    elif t["bpm"] is None:
        flags.append("No BPM")
    if t.get("bpm_from"):
        alt = t["bpm_from"]
        flags.append(f'BPM from other release ({(alt.get("album") or {}).get("title", "?")})')
    if t["bpm"] is not None and (t["bpm"] < TEMPO_LOW or t["bpm"] > TEMPO_HIGH):
        flags.append("Check tempo (half/double?)")
    if t["deezer"] and t["duration"] and t["deezer"].get("duration") and \
            abs(t["duration"] - t["deezer"]["duration"]) > matching.DURATION_TOLERANCE:
        diff = t["deezer"]["duration"] - t["duration"]
        flags.append(f"Duration differs ({'+' if diff > 0 else '-'}{fmt_dur(abs(diff))})")
    if t["method"] == "track search":
        flags.append("Matched via track search")
    return "; ".join(flags)


def deezer_label(t):
    dz = t["deezer"]
    return f'{dz.get("title", "")} — {(dz.get("artist") or {}).get("name", "")}' if dz else ""


def album_status(res):
    n = len(res["tracks"])
    with_bpm = sum(1 for t in res["tracks"] if t["bpm"] is not None)
    if res["dz_album"]:
        return f"OK – {with_bpm}/{n} tracks with BPM"
    return f"Deezer album not found – used track search ({with_bpm}/{n} with BPM)"


def print_album(row_label, res):
    dz = res["dz_album"]
    dz_txt = (f'{dz["title"]} — {dz["artist"]["name"]} (id {dz["id"]}, score {res["album_score"]})'
              if dz else f"not found (best score {res['album_score']})")
    print(f"\n=== {row_label}: {res['artist']} – {res['title']} ({res['year'] or 'no year'}) "
          f"| {res['folder']}")
    print(f"    Deezer album: {dz_txt}")
    print(f"    {'Pos':<5} {'Discogs title (dur)':<42} {'Deezer title — artist (dur)':<52} "
          f"{'score':>5}  {'method':<12} {'BPM':>6}")
    for t in res["tracks"]:
        left = f"{t['title'][:34]} ({fmt_dur(t['duration'])})"
        right = (f"{deezer_label(t)[:44]} ({fmt_dur(t['deezer'].get('duration'))})"
                 if t["deezer"] else "—")
        print(f"    {t['position']:<5} {left:<42} {right:<52} {t['score'] or '':>5}  "
              f"{t['method'] or '':<12} {t['bpm'] if t['bpm'] is not None else '—':>6}")
        if t.get("bpm_from"):
            alt = t["bpm_from"]
            print(f'          BPM from other release: {alt.get("title")} — '
                  f'{(alt.get("album") or {}).get("title", "?")} ({fmt_dur(alt.get("duration"))})')
        if t["credits"]:
            print(f"          credits: {t['credits'][:150]}")
    print(f"    Status: {album_status(res)}")


# --- Google Sheets -----------------------------------------------------------
def open_sheet():
    import gspread
    creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() or "google-service-account.json"
    sheet_id = (os.environ.get("BPM_SHEET_ID") or os.environ.get("BPM_GOOGLE_SHEET_ID") or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", sheet_id)   # accept a pasted sheet URL
    if m:
        sheet_id = m.group(1)
    if not sheet_id:
        sys.exit("Set BPM_SHEET_ID (or BPM_GOOGLE_SHEET_ID) in .env (see README).")
    creds_path = Path(creds).expanduser()
    if not creds_path.is_absolute():
        creds_path = config.PROJECT_DIR / creds_path
    if not creds_path.exists():
        sys.exit(f"Google service account key file not found: {creds_path.name} "
                 "(save it in this folder, or set GOOGLE_SERVICE_ACCOUNT_FILE; see README).")
    gc = gspread.service_account(filename=str(creds_path))
    try:
        return gc.open_by_key(sheet_id)
    except gspread.exceptions.SpreadsheetNotFound:
        sys.exit("Sheet not found. Check BPM_SHEET_ID and share the sheet with the "
                 "service account's email (Editor).")
    except gspread.exceptions.APIError as exc:
        sys.exit(f"Google Sheets error: {exc.response.status_code}. Is the sheet shared "
                 "with the service account?")


def read_albums(sh):
    ws = sh.worksheet(ALBUMS_TAB)
    values = ws.get_all_values()
    if not values:
        sys.exit(f'The "{ALBUMS_TAB}" tab is empty.')
    header = [h.strip().lower() for h in values[0]]
    col = {}
    for key, name in ALBUM_COLS.items():
        if name.lower() in header:
            col[key] = header.index(name.lower())
    if "id" not in col:
        sys.exit(f'"{ALBUMS_TAB}" needs a "{ALBUM_COLS["id"]}" column.')
    rows = []
    for r, vals in enumerate(values[1:], start=2):
        get = lambda k: vals[col[k]].strip() if k in col and col[k] < len(vals) else ""
        raw_id = get("id")
        m = re.search(r"\d+", raw_id)   # accepts "123", "r123" or a release URL
        if not m:
            continue
        rows.append({"row": r, "release_id": int(m.group()), "artist": get("artist"),
                     "album": get("album"), "notes": get("notes")})
    return ws, col, rows


def a1(row, col_idx):
    import gspread.utils
    return gspread.utils.rowcol_to_a1(row, col_idx + 1)


def write_back(albums_ws, col, updates):
    """updates: [(row, key, value)] -> one batch update on the Albums tab."""
    data = [{"range": a1(r, col[k]), "values": [[v]]} for r, k, v in updates if k in col]
    if data:
        albums_ws.batch_update(data, value_input_option="RAW")


def write_tracks(sh, rows):
    import gspread
    try:
        ws = sh.worksheet(TRACKS_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(TRACKS_TAB, rows=max(len(rows) + 10, 100), cols=len(TRACK_HEADER))
    ws.clear()
    ws.update([TRACK_HEADER] + rows, "A1", value_input_option="RAW")
    ws.format(f"A1:{a1(1, len(TRACK_HEADER) - 1)}", {"textFormat": {"bold": True}})
    ws.freeze(rows=1)


# --- main --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Look up per-track BPM on Deezer for Discogs releases.")
    ap.add_argument("--dry-run", action="store_true", help="print results; don't write to the sheet")
    ap.add_argument("--limit", type=int, metavar="N", help="only process the first N albums")
    ap.add_argument("--refresh", action="store_true", help="bypass the Discogs and Deezer caches")
    ap.add_argument("--ids", help="comma-separated Discogs release IDs to use instead of the sheet")
    args = ap.parse_args()

    load_dotenv(config.PROJECT_DIR / ".env")
    discogs = DiscogsClient()
    deezer = DeezerClient(refresh=args.refresh)

    sh = albums_ws = col = None
    if args.ids:
        albums = [{"row": None, "release_id": int(x), "artist": "", "album": "", "notes": ""}
                  for x in re.findall(r"\d+", args.ids)]
    else:
        sh = open_sheet()
        albums_ws, col, albums = read_albums(sh)
    if args.limit:
        albums = albums[:args.limit]
    print(f"{len(albums)} albums to process")

    folder_names = discogs.get_folders(cached=True, refresh=args.refresh)

    track_rows, updates = [], []
    n_tracks = n_matched = n_bpm = n_fallback = n_errors = 0
    for idx, alb in enumerate(albums, 1):
        label = f"Row {alb['row']}" if alb["row"] else f"#{idx}"
        try:
            res = process_album(alb["release_id"], discogs, deezer, folder_names, args.refresh)
        except (DiscogsError, DeezerError, ValueError, KeyError) as exc:
            n_errors += 1
            print(f"\n=== {label}: release {alb['release_id']} — Error: {exc}")
            updates.append((alb["row"], "status", f"Error: {exc}"))
            continue

        artist = alb["artist"] or res["artist"]   # never overwrite what you typed
        album = alb["album"] or res["title"]
        if not alb["artist"]:
            updates.append((alb["row"], "artist", res["artist"]))
        if not alb["album"]:
            updates.append((alb["row"], "album", res["title"]))
        updates.append((alb["row"], "status", album_status(res)))

        for t in res["tracks"]:
            n_tracks += 1
            n_matched += bool(t["deezer"])
            n_bpm += t["bpm"] is not None
            n_fallback += t["method"] == "track search"
            track_rows.append([artist, album, res["genres"], res["styles"], res["folder"],
                               res["year"], t["position"], t["title"],
                               t["bpm"] if t["bpm"] is not None else "", track_flags(t),
                               deezer_label(t), alb["notes"], t["credits"]])
        if args.dry_run:
            print_album(label, res)
        else:
            print(f"  {idx}/{len(albums)} {res['artist']} – {res['title']}: {album_status(res)}")

    if not args.dry_run and sh is not None:
        write_back(albums_ws, col, [u for u in updates if u[0]])
        write_tracks(sh, track_rows)
        print(f'\nWrote {len(track_rows)} rows to "{TRACKS_TAB}" and updated "{ALBUMS_TAB}".')
    elif not args.dry_run:
        print("\n(--ids given: nothing written to the sheet)")

    pct = lambda n: f"{100 * n / n_tracks:.0f}%" if n_tracks else "–"
    print(f"\nSummary: {len(albums)} albums ({n_errors} errors), {n_tracks} tracks, "
          f"{pct(n_matched)} matched to Deezer ({n_fallback} via track search), "
          f"{pct(n_bpm)} with BPM")
    calls = dict(discogs.calls_by_kind)
    print(f"API calls: Discogs {sum(calls.values())} {calls or ''}, "
          f"Deezer {sum(deezer.calls_by_kind.values())} {dict(deezer.calls_by_kind) or ''}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Cached responses are kept; rerun to resume.")
