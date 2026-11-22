# SongBPMLookup
Python script to lookup song BPMs from a list of albums
BPM data provided by [GetSongBPM](https://getsongbpm.com).

# Discogs Synth Finder

Finds synthesizer credits on the Funk / Soul and Jazz-Funk releases in your Discogs collection, then writes an Excel report.

## Setup (one time)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Put your Discogs personal access token in `.env`:

```
DISCOGS_TOKEN=your_token_here
```

You can create a token at https://www.discogs.com/settings/developers.

## Run

```bash
.venv/bin/python synth_finder.py --limit 10   # quick test on 10 releases
.venv/bin/python synth_finder.py              # full collection
.venv/bin/python synth_finder.py --refresh    # ignore the cache and refetch every release
```

- The first full run makes about one request per second, because Discogs allows 60 per minute. The progress line shows an ETA.
- Release details are cached in `cache/releases/`, so a rerun only fetches releases that aren't cached yet. If a run is interrupted, rerun it and it picks up where it stopped.
- The collection list itself (about 30 pages) is fetched fresh on every run, so newly added records are picked up.

## Output (`output/`)

- `synth_report.xlsx` has three sheets:
  - **Synth Matches**: one row per matching credit or notes snippet, with tier, source, credited person, track and Discogs link.
  - **Summary by Release**: one row per filtered release, with synth flags, players, the synth instruments named in the credits (e.g. ARP Odyssey, Minimoog, Mellotron) and match count. Releases with synths are sorted to the top. If any specific instrument is named, plain "Synthesizer" is left out. You can merge variant spellings with `INSTRUMENT_ALIASES` in `config.py`.
  - **Stats**: collection totals, top 15 synth players and keyword counts.
- `synth_matches.csv`, `summary_by_release.csv` and `stats.csv` hold the same data.

Both release sheets include:

- **folder:** your collection folder. If you own copies in more than one folder, every folder is listed, comma-separated.
- **One column per custom collection field** (Media Condition, Sleeve Condition, Amount Paid, Notes, …). If your copies have different values, they're joined with ` | `.
  - **Amount Paid** (and any other field in `NUMERIC_FIELDS` in `config.py`) is stored as a number, summed across copies. A value that isn't a number is kept as text, and the run prints a note.

The folder names and field names are looked up once per run (2 requests).

## What gets searched

1. Release credits (the role text, e.g. `Synthesizer [Moog]`)
2. Track credits, including sub-tracks
3. The release notes
4. Your own collection notes

Matching is case-insensitive and whole-word, so "arp" doesn't match "harp". It also accepts a trailing "s", so "synth" matches "Synths".

## Customising

Edit `config.py` to change the username, the genre and style filter, or the keyword lists:

- **STRONG** keywords mean a definite synth.
- **POSSIBLE** keywords are ambiguous keyboards (Rhodes, organ and so on).

Some STRONG keywords are also names or other things. For example, "Roland" can be Roland Kirk, and "ARP" can be the American Record Pressing plant. In release notes, `RELEASE_NOTES_EXCLUSIONS` skips a keyword when the nearby text matches a pattern. Out of the box it skips "ARP" near words like *stamped*, *pressed* or *runouts*, and "Roland" when it's part of the names Roland Kirk or Roland Bautista. Add your own patterns there if you spot other false positives.

---

# Deezer BPM proof of concept (`deezer_bpm_poc.py`)

This script reads Discogs release IDs from a Google Sheet. For each release it builds the track list from Discogs, looks up each track's BPM on Deezer, and writes one row per track to a **Tracks** tab.

## Google service account setup (personal Google account, one time)

1. Go to https://console.cloud.google.com/ and create a project (any name, e.g. "discogs-bpm").
2. Open **APIs & Services → Library**, then search for and **Enable** both the **Google Sheets API** and the **Google Drive API**.
3. Open **APIs & Services → Credentials → Create credentials → Service account**. Give it a name, skip the optional steps and click **Done**.
4. Click the new service account, then **Keys → Add key → Create new key → JSON**. A JSON file downloads.
5. Move that file into this folder as `google-service-account.json`. It's gitignored. Don't share it, because it works like a password.
6. Open the JSON file and copy the `client_email` value (it looks like `name@project.iam.gserviceaccount.com`). In your Google Sheet, click **Share**, paste that address and give it **Editor** access.
7. In `.env`, set the sheet ID. `BPM_SHEET_ID` and `BPM_GOOGLE_SHEET_ID` both work, and you can paste the full sheet URL instead of the ID:
   ```
   BPM_GOOGLE_SHEET_ID=<the ID in the sheet URL, between /d/ and /edit>
   ```
   `GOOGLE_SERVICE_ACCOUNT_FILE` is optional. It defaults to `google-service-account.json` in this folder.

   Note that a Google **API key** (`AIza…`) won't work here. API keys can only read public sheets, and this script writes to your sheet.

## Sheet layout

**Albums** tab (input). The header row needs these columns, in any order:

`Discogs Release ID | Artist | Album | Personal Notes | Status`

- **Discogs Release ID** is the only required column. Rows without one are skipped.
- A blank Artist or Album is filled in from Discogs. Values you've typed are never changed.
- **Status** is written by the script. For example: `OK – 8/9 tracks with BPM`, `Deezer album not found – used track search (…)` or `Error: …`.

**Tracks** tab (output). It's created if missing, then cleared and rewritten on every run:

`Artist | Album | Genre | Style | Folder | Year | Position | Track Name | BPM | Flag | Deezer Track | Personal Notes | Credits`

- **Year** is the original release year (from the Discogs master release) when there is one.
- **Folder** lists the collection folder of every copy you own, or "Not in collection".
- **Deezer Track** shows exactly which Deezer track was matched, so you can spot bad matches.
- **Credits** lists the Discogs credits for that track, grouped by person, e.g. `Bruce Fisher (Solo Vocal, Written-By); Quincy Jones (Written-By)`. It includes the track's own credits plus album credits that name specific tracks ("A1 to A3"). Credits for the whole album are left out so they don't repeat on every row.
- **Flag** can be: `No Deezer match`, `No BPM`, `Check tempo (half/double?)` (below 70 or above 160 BPM), `BPM from other release (<album>)`, `Duration differs (±m:ss)` (Deezer's track is more than 15 seconds longer or shorter than the Discogs duration, which usually means a different edit) and `Matched via track search`.

## Run

```bash
.venv/bin/python deezer_bpm_poc.py --dry-run --limit 2   # print matches, write nothing
.venv/bin/python deezer_bpm_poc.py --limit 5             # write the first 5 albums
.venv/bin/python deezer_bpm_poc.py                       # all albums
.venv/bin/python deezer_bpm_poc.py --dry-run --ids 766244,5103085   # test IDs without the sheet
```

- `--refresh` ignores the cache and refetches everything from Discogs and Deezer.
- All Discogs and Deezer responses are cached under `cache/`, so reruns are fast and make almost no requests. Use `--refresh` after you move records between folders.
- Every run ends with a summary: albums, tracks, % matched to Deezer, % with BPM, and the number of requests made.

## How matching works

1. **Album search:** Deezer is searched for the album, and the result must score at least 80 on both artist and title. Remaster and edition tags like "(Deluxe Edition)" are ignored.
2. **Track matching:** each Discogs track is matched to a track on that album by title (score at least 85). Duration (within 15 seconds) breaks ties, and each Deezer track is used once.
3. **Track search fallback:** any track still unmatched is searched for on its own, and the result must also pass an artist check.
4. **BPM:** a Deezer BPM of 0 or missing counts as no data. In that case the script looks for the same recording on another Deezer release, such as a compilation. A candidate must have the same title (score at least 85), the same artist, and a duration within 15 seconds of your Discogs pressing or of the matched Deezer track. If one has a BPM, it's used and the row is flagged `BPM from other release (<album>)`. Otherwise the row is flagged "No BPM".

The thresholds are at the top of `matching.py`.

**Known limitation:** Deezer stores BPM per release, not per song. The same song often has BPM 0 on the original album but a value on a compilation. Deezer's BPM values are also sometimes double the real tempo, which is what the "Check tempo" flag is for.
