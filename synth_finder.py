"""Find synthesizer credits in a Discogs collection.

Usage:
    python synth_finder.py               # full run
    python synth_finder.py --limit 10    # test run on the first 10 filtered releases
    python synth_finder.py --refresh     # ignore the release cache and refetch
"""

import argparse
import sys
import time

import config
import report
from detect import find_matches
from discogs_api import DiscogsClient, DiscogsError


def fmt_secs(s):
    s = int(round(s))
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60}m"
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


def is_target(item):
    return any(g in item["genres"] for g in config.FILTER_GENRES) or \
           any(s in item["styles"] for s in config.FILTER_STYLES)


def main():
    ap = argparse.ArgumentParser(description="Find synth credits in your Discogs collection.")
    ap.add_argument("--refresh", action="store_true", help="ignore cached release data and refetch")
    ap.add_argument("--limit", type=int, metavar="N", help="only process the first N filtered releases")
    args = ap.parse_args()

    client = DiscogsClient()

    print(f"Fetching collection for {config.USERNAME}…")
    collection = client.get_collection()
    print(f"  {len(collection)} items in collection")

    folder_names = client.get_folders()
    fields = client.get_fields()
    field_names = {f["id"]: f["name"] for f in fields}
    print(f"  {len(folder_names)} folders, {len(fields)} custom fields "
          f"({', '.join(field_names.values())})")

    # Merge multiple copies of the same release: keep every folder and every copy's notes.
    by_id = {}
    for item in collection:
        if not is_target(item):
            continue
        merged = by_id.get(item["id"])
        if merged is None:
            by_id[item["id"]] = {**item, "folder_ids": [item["folder_id"]],
                                 "notes": list(item["notes"])}
        else:
            merged["folder_ids"].append(item["folder_id"])
            merged["notes"].extend(item["notes"])
    filtered = list(by_id.values())
    print(f"  {len(filtered)} releases match the filter "
          f"({', '.join(config.FILTER_GENRES + config.FILTER_STYLES)})")
    filtered_total = len(filtered)
    if args.limit:
        filtered = filtered[:args.limit]
        print(f"  --limit {args.limit}: processing {len(filtered)} releases")

    to_check = [it["id"] for it in filtered]
    uncached_total = sum(1 for rid in to_check
                         if args.refresh or not (config.CACHE_DIR / f"{rid}.json").exists())
    print(f"  {len(to_check) - uncached_total} cached, {uncached_total} to fetch\n")

    matches_by_id, missing = {}, []
    fetched, cached, fetch_time = 0, 0, 0.0
    total = len(filtered)
    for i, item in enumerate(filtered, 1):
        t0 = time.monotonic()
        release, from_cache = client.get_release(item["id"], refresh=args.refresh)
        if from_cache:
            cached += 1
        else:
            fetched += 1
            fetch_time += time.monotonic() - t0
        if release is None:
            missing.append(item["id"])
        matches_by_id[item["id"]] = find_matches(release, item, field_names)

        remaining_uncached = uncached_total - fetched
        eta = (fetch_time / fetched * remaining_uncached) if fetched else remaining_uncached * 1.1
        print(f"\rFetching {i}/{total} (cached: {cached}) — ETA {fmt_secs(eta)}   ",
              end="", flush=True)
    print()

    if missing:
        print(f"  {len(missing)} releases not found on Discogs (404): {missing}")

    matches_df, summary_df, stats_df = report.build(len(collection), filtered_total, filtered, matches_by_id,
                                                    folder_names, fields)
    xlsx = report.write(matches_df, summary_df, stats_df)

    print(f"\nAPI calls this run: {client.api_calls} "
          f"({', '.join(f'{k}: {v}' for k, v in client.calls_by_kind.items())})")
    print(f"Report: {xlsx}")
    print(f"CSVs:   {config.OUTPUT_DIR}/synth_matches.csv, summary_by_release.csv, stats.csv")
    print("\n" + stats_df[stats_df["section"] == "overview"][["label", "value"]]
          .to_string(index=False, header=False))


if __name__ == "__main__":
    try:
        main()
    except DiscogsError as exc:
        sys.exit(f"\nError: {exc}")
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Cached releases are kept; rerun to resume.")
