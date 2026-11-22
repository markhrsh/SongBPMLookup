"""Build report DataFrames and write the Excel workbook and CSVs."""

import re
from collections import Counter

import pandas as pd

import config
from detect import STRONG, POSSIBLE, clean_name, merge_instruments

BASE_COLS = ["release_id", "artist", "title", "folder", "year", "label", "genres", "styles"]
MATCH_ONLY_COLS = ["match_tier", "source", "matched_text", "matched_keywords",
                   "credited_person", "track_position", "discogs_url"]


def field_columns(fields):
    """Map field id -> unique column name for each custom collection field."""
    cols, taken = {}, set(BASE_COLS + MATCH_ONLY_COLS)
    for f in fields:
        name = f["name"]
        if name.lower() in taken:
            name = f"my {name}"
        taken.add(name.lower())
        cols[f["id"]] = name
    return cols


def parse_number(value):
    """'1.00', '$35', '1,200.50' -> float; returns None if it isn't a number."""
    text = re.sub(r"[^\d.\-]", "", str(value))
    try:
        return float(text) if text else None
    except ValueError:
        return None


def numeric_value(values):
    """Sum per-copy values; if any value isn't a number, keep the raw text."""
    nums = [parse_number(v) for v in values]
    if all(n is not None for n in nums):
        return round(sum(nums), 2)
    return " | ".join(values)


def release_meta(item, folder_names, field_cols, numeric_ids=frozenset()):
    folders = dict.fromkeys(folder_names.get(fid, f"Folder {fid}") for fid in item["folder_ids"])
    meta = {
        "release_id": item["id"],
        "artist": ", ".join(clean_name(a) for a in item["artists"]),
        "title": item["title"],
        "folder": ", ".join(folders),
        "year": item["year"] or "",
        "label": ", ".join(dict.fromkeys(l.get("name", "") for l in item["labels"])),
        "genres": ", ".join(item["genres"]),
        "styles": ", ".join(item["styles"]),
    }
    for fid, col in field_cols.items():
        if fid in numeric_ids:
            per_copy = [str(n.get("value")).strip() for n in item["notes"]
                        if n.get("field_id") == fid and str(n.get("value") or "").strip()]
            meta[col] = numeric_value(per_copy) if per_copy else None
            continue
        values = dict.fromkeys(str(n.get("value")).strip() for n in item["notes"]
                               if n.get("field_id") == fid and str(n.get("value") or "").strip())
        meta[col] = " | ".join(values)
    meta["discogs_url"] = f"https://www.discogs.com/release/{item['id']}"
    return meta


def build(collection_size, filtered_total, filtered_items, matches_by_id, folder_names, fields):
    field_cols = field_columns(fields)
    numeric_ids = {f["id"] for f in fields if f["name"] in config.NUMERIC_FIELDS}
    numeric_cols = [field_cols[i] for i in numeric_ids]
    meta_cols = BASE_COLS + list(field_cols.values())
    match_cols = meta_cols + MATCH_ONLY_COLS
    match_rows, summary_rows = [], []
    for item in filtered_items:
        meta = release_meta(item, folder_names, field_cols, numeric_ids)
        ms = matches_by_id.get(item["id"], [])
        for m in ms:
            match_rows.append({**meta, **m})
        strong_players = list(dict.fromkeys(
            m["credited_person"] for m in ms if m["match_tier"] == STRONG and m["credited_person"]))
        possible_players = [p for p in dict.fromkeys(
            m["credited_person"] for m in ms if m["match_tier"] == POSSIBLE and m["credited_person"])
            if p not in strong_players]
        summary_rows.append({
            **{c: meta[c] for c in meta_cols},
            "has_strong_synth": any(m["match_tier"] == STRONG for m in ms),
            "has_possible_synth": any(m["match_tier"] == POSSIBLE for m in ms),
            "synth_players": ", ".join(strong_players),
            "synth_instruments": ", ".join(merge_instruments(
                m.get("instruments", []) for m in ms if m["match_tier"] == STRONG)),
            "possible_players": ", ".join(possible_players),
            "match_count": len(ms),
            "discogs_url": meta["discogs_url"],
        })

    matches_df = pd.DataFrame(match_rows, columns=match_cols)
    summary_df = pd.DataFrame(summary_rows)
    if not summary_df.empty:
        summary_df = summary_df.sort_values(
            ["has_strong_synth", "has_possible_synth", "match_count", "artist"],
            ascending=[False, False, False, True], kind="stable").reset_index(drop=True)

    # --- stats ---
    n_strong = int(summary_df["has_strong_synth"].sum()) if not summary_df.empty else 0
    n_possible = int(summary_df["has_possible_synth"].sum()) if not summary_df.empty else 0
    n_possible_only = int((summary_df["has_possible_synth"] & ~summary_df["has_strong_synth"]).sum()) \
        if not summary_df.empty else 0
    n_any = int((summary_df["match_count"] > 0).sum()) if not summary_df.empty else 0

    player_releases = Counter()
    kw_releases = {STRONG: Counter(), POSSIBLE: Counter()}
    for rid, ms in matches_by_id.items():
        for p in {m["credited_person"] for m in ms if m["match_tier"] == STRONG and m["credited_person"]}:
            player_releases[p] += 1
        kws = set()
        for m in ms:
            for kw in m["matched_keywords"].split(", "):
                tier = STRONG if kw in [k.lower() for k in config.STRONG_KEYWORDS] else POSSIBLE
                kws.add((tier, kw))
        for tier, kw in kws:
            kw_releases[tier][kw] += 1

    stats = [("overview", "total collection size", collection_size),
             ("overview", "filtered releases (Funk / Soul or Jazz-Funk)", filtered_total),
             ("overview", "releases processed this run", len(filtered_items)),
             ("overview", "releases with STRONG match", n_strong),
             ("overview", "releases with POSSIBLE match", n_possible),
             ("overview", "releases with POSSIBLE match only", n_possible_only),
             ("overview", "releases with any match", n_any),
             ("overview", "total match rows", len(match_rows))]
    stats += [("top synth players (releases, STRONG)", p, c)
              for p, c in player_releases.most_common(config.TOP_PLAYERS)]
    for tier in (STRONG, POSSIBLE):
        stats += [(f"top keywords - {tier} (releases)", kw, c)
                  for kw, c in kw_releases[tier].most_common()]
    stats_df = pd.DataFrame(stats, columns=["section", "label", "value"])
    for col in numeric_cols:
        bad = summary_df[col].map(lambda v: isinstance(v, str)).sum() if col in summary_df else 0
        if bad:
            print(f"  note: {bad} '{col}' values aren't numbers and were kept as text")
    matches_df.attrs["numeric_cols"] = summary_df.attrs["numeric_cols"] = numeric_cols
    return matches_df, summary_df, stats_df


def _format_sheet(ws, numeric_cols=()):
    from openpyxl.styles import Font
    for cell in ws[1]:
        cell.font = Font(bold=True)
        if cell.value in numeric_cols:
            for row in ws.iter_rows(min_row=2, min_col=cell.column, max_col=cell.column):
                if isinstance(row[0].value, (int, float)):
                    row[0].number_format = "#,##0.00"
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 8), 60)


def write(matches_df, summary_df, stats_df):
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    xlsx = config.OUTPUT_DIR / "synth_report.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as xw:
        matches_df.to_excel(xw, sheet_name="Synth Matches", index=False)
        summary_df.to_excel(xw, sheet_name="Summary by Release", index=False)
        stats_df.to_excel(xw, sheet_name="Stats", index=False)
        numeric_cols = summary_df.attrs.get("numeric_cols", [])
        for ws in xw.book.worksheets:
            _format_sheet(ws, numeric_cols if ws.title != "Stats" else ())
    matches_df.to_csv(config.OUTPUT_DIR / "synth_matches.csv", index=False)
    summary_df.to_csv(config.OUTPUT_DIR / "summary_by_release.csv", index=False)
    stats_df.to_csv(config.OUTPUT_DIR / "stats.csv", index=False)
    return xlsx
