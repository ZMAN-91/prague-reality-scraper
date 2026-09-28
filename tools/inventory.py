"""Gather the facts an inventory of the whole collection is judged on.

    python -m tools.inventory --data-dir store/data --logs-dir store/logs \
        --out /tmp/inventura [--repo store] [--github]

Read-only. It changes nothing in the dataset and writes only into --out:
inventory.json (every number) and inventory.md (the same, readable). The
output describes a private dataset, so --out must not be inside this public
repository - the tool refuses a path under it.

What it measures, section by section:

  listings    counts by source, status and category; how full every column
              is per source; values that cannot be right (areas, floors,
              coordinates outside Prague); files and their sizes; duplicates.
  lifecycle   what the status machine did: how many removed listings came
              back (and after how long) - the measured false-removal rate -,
              time on market, missing -> active churn per source.
  runs        the run log: runs per day and kind, durations, what each
              source fetched and marked, errors, planned stops, silences.
  prices      latest known price per listing: medians and spreads per
              segment and district with sample sizes and bootstrap intervals,
              values no market has, and how far two portals disagree on the
              price of the same flat.
  series      the daily market series: how many listings stand behind each
              figure and how much a figure moves from day to day with nothing
              happening - the smallest change the series can actually show.
  raw         every field path the portals send, with how often it is filled,
              so what is thrown away can be weighed against what is kept.
  growth      rows and bytes per day, and where three years of that leads:
              file sizes, repository size, the memory and time it takes to
              load every listing ever seen - which every run does.
  actions     (--github) runs per workflow, conclusions, queue waits and
              durations from the GitHub API, for both repositories.
"""

from __future__ import annotations

import argparse
import collections
import csv
import gzip
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from common import storage
from common.schema import LISTING_FIELDS, STATUS_ACTIVE, STATUS_REMOVED

REPO_ROOT = Path(__file__).resolve().parent.parent

# Prague's administrative boundary sits inside this box; anything outside
# it is a coordinate that cannot belong to a Prague listing.
PRAGUE_BBOX = (49.94, 50.18, 14.22, 14.71)  # lat_min, lat_max, lon_min, lon_max

YEARS_AHEAD = 3


# --- small statistics ---------------------------------------------------------


def quantiles(values: list, points=(0.1, 0.25, 0.5, 0.75, 0.9)) -> dict:
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"n": 0}
    out = {"n": len(values)}
    for p in points:
        k = (len(values) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        out[f"p{int(p * 100)}"] = values[lo] + (values[hi] - values[lo]) * (k - lo)
    return out


def bootstrap_median_ci(values: list, rounds: int = 400, seed: int = 7) -> Optional[tuple]:
    """95 % interval of the median. Deterministic (fixed seed) so two runs of
    the inventory on the same data print the same numbers."""
    values = [v for v in values if v is not None]
    if len(values) < 5:
        return None
    rng = random.Random(seed)
    medians = sorted(statistics.median(rng.choices(values, k=len(values)))
                     for _ in range(rounds))
    return medians[int(rounds * 0.025)], medians[int(rounds * 0.975) - 1]


def as_float(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def day(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# --- loading ------------------------------------------------------------------


def load_observations(data_dir: Path) -> list:
    rows = []
    for path in sorted((data_dir / "observations").glob("*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    return rows


def load_changes(data_dir: Path) -> list:
    rows = []
    for path in sorted((data_dir / "changes").glob("*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    return rows


def load_runs(logs_dir: Path) -> list:
    runs = []
    for path in sorted(logs_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    runs.append(json.loads(line))
                except ValueError:
                    runs.append({"unparseable": True, "file": path.name})
    return runs


# --- listings -----------------------------------------------------------------


def listings_profile(listings: dict, data_dir: Path) -> dict:
    by_source = collections.defaultdict(list)
    for row in listings.values():
        by_source[row.get("source", "")].append(row)

    counts = collections.Counter(
        (r.get("source"), r.get("property_type"), r.get("transaction_type"), r.get("status"))
        for r in listings.values())

    fill = {}
    for source, rows in sorted(by_source.items()):
        fill[source] = {
            field: round(100 * sum(1 for r in rows if str(r.get(field, "")).strip()) / len(rows), 1)
            for field in LISTING_FIELDS
        }

    implausible = collections.Counter()
    examples = collections.defaultdict(list)

    def flag(kind, row):
        implausible[kind] += 1
        if len(examples[kind]) < 5:
            examples[kind].append(row.get("url") or row.get("internal_id"))

    for row in listings.values():
        area = as_float(row.get("area_m2"))
        if area is not None and (area < 12 or area > 800):
            flag("area_outside_12_800_m2", row)
        floor = as_float(row.get("floor"))
        if floor is not None and (floor < -3 or floor > 40):
            flag("floor_outside_-3_40", row)
        lat, lon = as_float(row.get("lat")), as_float(row.get("lon"))
        if lat is not None and lon is not None:
            if not (PRAGUE_BBOX[0] <= lat <= PRAGUE_BBOX[1] and PRAGUE_BBOX[2] <= lon <= PRAGUE_BBOX[3]):
                flag("coordinates_outside_prague_box", row)
        first, last = day(row.get("first_seen_at")), day(row.get("last_seen_at"))
        if first and last and last < first:
            flag("last_seen_before_first_seen", row)
        if row.get("status") not in (STATUS_ACTIVE, STATUS_REMOVED) and \
                not str(row.get("status", "")).startswith("missing_"):
            flag("unknown_status", row)
        if row.get("relisted_from") and row["relisted_from"] not in listings:
            flag("relisted_from_points_nowhere", row)

    main = data_dir / "listings.csv"
    files = [{"path": str(p.relative_to(data_dir)), "bytes": p.stat().st_size,
              "rows": sum(1 for _ in open(p, encoding="utf-8")) - 1}
             for p in storage.listing_files(main)]
    cluster_sizes = collections.Counter(
        r["cluster_id"] for r in listings.values() if r.get("cluster_id"))
    cluster_sources = collections.defaultdict(set)
    for r in listings.values():
        if r.get("cluster_id"):
            cluster_sources[r["cluster_id"]].add(r.get("source"))
    return {
        "total": len(listings),
        "counts": [{"source": s, "type": t, "transaction": x, "status": st, "n": n}
                   for (s, t, x, st), n in sorted(counts.items(), key=lambda kv: [str(k) for k in kv[0]])],
        "fill_pct": fill,
        "implausible": dict(implausible),
        "implausible_examples": dict(examples),
        "files": files,
        "duplicates_across_files": storage.duplicate_listings(main),
        "clusters": {
            "count": len(cluster_sizes),
            "size_distribution": dict(collections.Counter(cluster_sizes.values())),
            "cross_portal": sum(1 for s in cluster_sources.values() if len(s) > 1),
            "dedup_confidence": dict(collections.Counter(
                r.get("dedup_confidence") or "" for r in listings.values() if r.get("cluster_id"))),
        },
        "relisted": sum(1 for r in listings.values() if r.get("relisted_from")),
    }


# --- lifecycle ----------------------------------------------------------------


def lifecycle(listings: dict, observations: list) -> dict:
    """What the status machine did, measured from the observation log."""
    by_id = collections.defaultdict(list)
    for o in observations:
        by_id[o["internal_id"]].append(o)
    for rows in by_id.values():
        rows.sort(key=lambda o: o["observed_at"])

    per_source = collections.defaultdict(lambda: collections.Counter())
    return_gaps = collections.defaultdict(list)
    for internal_id, rows in by_id.items():
        source = listings.get(internal_id, {}).get("source", "?")
        previous = None
        removed_at = None
        for o in rows:
            status = o["status"]
            if status == STATUS_REMOVED and previous != STATUS_REMOVED:
                per_source[source]["removals"] += 1
                removed_at = o["observed_at"]
            if status == STATUS_ACTIVE and previous == STATUS_REMOVED:
                per_source[source]["returned_after_removal"] += 1
                a, b = day(removed_at), day(o["observed_at"])
                if a and b:
                    return_gaps[source].append((b - a).days)
            if status == STATUS_ACTIVE and previous and previous.startswith("missing_"):
                per_source[source]["returned_while_missing"] += 1
            if status.startswith("missing_") and previous == STATUS_ACTIVE:
                per_source[source]["went_missing"] += 1
            previous = status

    time_on_market = collections.defaultdict(list)
    for r in listings.values():
        if r.get("status") == STATUS_REMOVED:
            first, last = day(r.get("first_seen_at")), day(r.get("last_seen_at"))
            if first and last:
                time_on_market[(r.get("source"), r.get("transaction_type"))].append((last - first).days)

    out = {}
    for source, c in sorted(per_source.items()):
        removals = c["removals"]
        out[source] = {
            **dict(c),
            "false_removal_pct": round(100 * c["returned_after_removal"] / removals, 2) if removals else None,
            "missing_that_came_back_pct": round(100 * c["returned_while_missing"] / c["went_missing"], 1)
            if c["went_missing"] else None,
            "days_until_return": quantiles(return_gaps[source]),
        }
    return {
        "per_source": out,
        "time_on_market_days_of_removed": {f"{s}/{t}": quantiles(v)
                                           for (s, t), v in sorted(time_on_market.items(), key=str)},
        "note": "Observed from the first observation on; lifetimes of listings "
                "already on the market when collection began are left-censored.",
    }


# --- runs ---------------------------------------------------------------------


def runs_profile(runs: list) -> dict:
    per_day = collections.defaultdict(collections.Counter)
    durations = collections.defaultdict(list)
    per_source_day = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    errors = collections.Counter()
    planned = collections.Counter()
    not_ok = []
    starts = []
    for run in runs:
        if run.get("unparseable"):
            errors["unparseable log line"] += 1
            continue
        started = run.get("started_at", "")
        kind = run.get("kind") or f"sweep/{run.get('scope') or 'unscoped'}"
        per_day[started[:10]][kind] += 1
        begun = storage_time(started)
        ended = storage_time(run.get("finished_at", ""))
        if begun and ended:
            durations[kind].append((ended - begun).total_seconds() / 60)
        if begun and not run.get("kind"):
            starts.append(begun)
        if run.get("ok") is False:
            not_ok.append(started)
        sources = run.get("sources")
        if isinstance(sources, dict):
            for name, stats in sources.items():
                bucket = per_source_day[name][started[:10]]
                for key in ("fetched", "new", "updated", "reactivated",
                            "missing_marked", "removed_confirmed"):
                    bucket[key] += stats.get(key, 0) or 0
                bucket["runs"] += 1
                for message in stats.get("errors") or []:
                    errors[f"{name}: {str(message)[:90]}"] += 1
                for message in stats.get("interruptions") or []:
                    planned[f"{name}: {str(message)[:90]}"] += 1
    starts.sort()
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(starts, starts[1:])]
    return {
        "runs": len(runs),
        "per_day": {d: dict(c) for d, c in sorted(per_day.items())},
        "duration_minutes": {k: quantiles(v, (0.5, 0.9, 1.0)) for k, v in durations.items()},
        "per_source_per_day": {s: {d: dict(c) for d, c in sorted(days.items())}
                               for s, days in per_source_day.items()},
        "errors": dict(errors.most_common(30)),
        "planned_stops": dict(planned.most_common(30)),
        "runs_not_ok": not_ok[-30:],
        "sweep_gaps_hours": quantiles(gaps, (0.5, 0.9, 0.99, 1.0)),
        "longest_silences": sorted(
            ((round(g, 2), a.isoformat()) for g, a in zip(gaps, starts)), reverse=True)[:5],
    }


def storage_time(value: str) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# --- prices -------------------------------------------------------------------


def prices(listings: dict, data_dir: Path) -> dict:
    state = storage.read_last_observation_state(data_dir / "state" / "last_observation.json")
    live = [r for r in listings.values() if r.get("status") != STATUS_REMOVED]

    def price_of(row):
        return as_float((state.get(row["internal_id"]) or {}).get("price"))

    segments = collections.defaultdict(lambda: {"price": [], "pm2": []})
    by_district = collections.defaultdict(list)
    absurd = collections.Counter()
    absurd_examples = collections.defaultdict(list)
    no_price = collections.Counter()
    for row in live:
        segment = f"{row.get('property_type')}/{row.get('transaction_type')}"
        price, area = price_of(row), as_float(row.get("area_m2"))
        if price is None:
            no_price[f"{row.get('source')}/{segment}"] += 1
            continue
        segments[segment]["price"].append(price)
        pm2 = price / area if area and area > 0 else None
        if pm2 is not None:
            segments[segment]["pm2"].append(pm2)
            if row.get("mestska_cast"):
                by_district[(segment, row["mestska_cast"])].append(pm2)
        rent = row.get("transaction_type") == "pronajem"
        low, high = (2_000, 300_000) if rent else (300_000, 300_000_000)
        if not (low <= price <= high):
            key = f"{segment} price outside {low}-{high}"
            absurd[key] += 1
            if len(absurd_examples[key]) < 5:
                absurd_examples[key].append((row.get("url"), price))
        if pm2 is not None:
            low, high = (80, 2_000) if rent else (20_000, 600_000)
            if not (low <= pm2 <= high):
                key = f"{segment} price/m2 outside {low}-{high}"
                absurd[key] += 1
                if len(absurd_examples[key]) < 5:
                    absurd_examples[key].append((row.get("url"), round(pm2)))

    districts = {}
    for (segment, district), values in by_district.items():
        if len(values) >= 5:
            ci = bootstrap_median_ci(values)
            districts.setdefault(segment, []).append({
                "district": district, "n": len(values),
                "median_pm2": round(statistics.median(values)),
                "ci95": [round(ci[0]), round(ci[1])] if ci else None,
                "ci_width_pct": round(100 * (ci[1] - ci[0]) / statistics.median(values), 1) if ci else None,
            })
    for rows in districts.values():
        rows.sort(key=lambda r: -r["n"])

    # The same flat on two portals: how far apart are the prices?
    by_cluster = collections.defaultdict(list)
    for row in live:
        if row.get("cluster_id"):
            price = price_of(row)
            if price:
                by_cluster[row["cluster_id"]].append((row.get("source"), price))
    spreads, same_price = [], 0
    for members in by_cluster.values():
        if len({s for s, _ in members}) > 1:
            values = [p for _, p in members]
            spreads.append((max(values) - min(values)) / min(values) * 100)
            same_price += max(values) == min(values)
    # Near misses: the same place, layout and size on two portals, left
    # unpaired only because the prices differ. Pairing requires a price the
    # two adverts shared (common/dedup.py), so paired prices agree by
    # construction and say nothing; these are the question worth asking -
    # two units in one development, or one flat priced differently per portal?
    spots = collections.defaultdict(list)
    for row in live:
        lat, lon, area = as_float(row.get("lat")), as_float(row.get("lon")), as_float(row.get("area_m2"))
        if lat is None or lon is None or area is None or not row.get("disposition"):
            continue
        spots[(round(lat, 4), round(lon, 4), row["disposition"], row.get("transaction_type"))].append(row)
    near_miss = []
    for rows in spots.values():
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if a.get("source") == b.get("source"):
                    continue
                if a.get("cluster_id") and a.get("cluster_id") == b.get("cluster_id"):
                    continue
                if abs(as_float(a["area_m2"]) - as_float(b["area_m2"])) > 1:
                    continue
                pa, pb = price_of(a), price_of(b)
                if pa and pb and pa != pb:
                    near_miss.append(abs(pa - pb) / min(pa, pb) * 100)
    return {
        "segments": {s: {"price": quantiles(v["price"]), "price_per_m2": quantiles(v["pm2"])}
                     for s, v in sorted(segments.items())},
        "districts_price_per_m2": districts,
        "live_without_price": dict(no_price),
        "implausible": dict(absurd),
        "implausible_examples": dict(absurd_examples),
        "cross_portal_clusters": {
            "n": len(spreads), "identical_price": same_price,
            "note": "identical by construction: pairing requires a shared price",
        },
        "unpaired_near_misses": {
            "pairs": len(near_miss),
            "price_difference_pct": quantiles(near_miss, (0.1, 0.25, 0.5, 0.75, 0.9)),
            "note": "same coordinates (4 decimals), layout, transaction and area "
                    "within 1 m2 on two portals, not paired because the price differs",
        },
    }


# --- the daily series ------------------------------------------------------------


def series_quality(data_dir: Path) -> dict:
    path = data_dir / "csv" / "trh_denne.csv"
    if not path.exists():
        return {"missing": str(path)}
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = {}
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["segment"], r["okno_dnu"])].append(r)
    for (segment, window), group in sorted(groups.items()):
        group.sort(key=lambda r: r["den"])
        entry = {"days": len(group), "complete_days": sum(r.get("den_uplny") == "ano" for r in group)}
        for field in ("nabidka", "cena_median", "cena_m2_median", "nove_denne", "zmizele_denne"):
            values = [as_float(r.get(field)) for r in group if r.get("den_uplny") == "ano"]
            values = [v for v in values if v is not None]
            changes = [abs(b - a) / a * 100 for a, b in zip(values, values[1:]) if a]
            entry[field] = {
                "last": values[-1] if values else None,
                "day_to_day_change_pct": quantiles(changes, (0.5, 0.9)),
            }
        out[f"{segment}@{window}d"] = entry
    return {
        "per_segment": out,
        "note": "The median day-to-day move of a median price is roughly the "
                "smallest change the series can distinguish from noise; a "
                "weekly move smaller than ~2x that is not a finding.",
    }


# --- raw payloads --------------------------------------------------------------


def field_paths(obj, prefix: str = "", out: Optional[collections.Counter] = None,
                depth: int = 0) -> collections.Counter:
    """Every key path in a JSON object, lists flattened to their items."""
    out = out if out is not None else collections.Counter()
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else key
            if value not in (None, "", [], {}):
                out[path] += 1
            field_paths(value, path, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            field_paths(item, prefix + "[]", out, depth + 1)
    elif isinstance(obj, str) and obj[:1] in "{[":
        try:
            field_paths(json.loads(obj), prefix + "<json>", out, depth + 1)
        except ValueError:
            pass
    return out


def raw_fields(data_dir: Path, days: int = 2) -> dict:
    """Field paths the portals send, per source and payload kind, with the
    share of records that carry each - from the newest `days` days."""
    out = {}
    raw = data_dir / "raw"
    for source_dir in sorted(p for p in raw.glob("*") if p.is_dir()):
        for day_dir in sorted(p for p in source_dir.glob("*") if p.is_dir())[-days:]:
            for path in sorted(day_dir.glob("*.json.gz")):
                kind = path.name.split("-")[0]
                try:
                    payload = json.load(gzip.open(path))
                except (OSError, ValueError):
                    continue
                pages = payload.get("pages", payload) if isinstance(payload, dict) else payload
                records = []
                for page in pages if isinstance(pages, list) else []:
                    response = page.get("response") if isinstance(page, dict) else None
                    if isinstance(response, dict) and isinstance(response.get("results"), list):
                        records.extend(response["results"])
                    elif isinstance(response, dict):
                        records.append(response)
                bucket = out.setdefault(f"{source_dir.name}/{kind}", {"records": 0, "paths": collections.Counter()})
                bucket["records"] += len(records)
                for record in records:
                    field_paths(record, out=bucket["paths"])
    return {
        key: {"records": v["records"],
              "fields": {p: round(100 * n / v["records"], 1)
                         for p, n in sorted(v["paths"].items()) if v["records"]}}
        for key, v in out.items()
    }


# --- growth and three years out -------------------------------------------------


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def growth(listings: dict, observations: list, changes: list, data_dir: Path,
           logs_dir: Path, repo: Optional[Path]) -> dict:
    first_days = collections.Counter(str(r.get("first_seen_at", ""))[:10] for r in listings.values())
    obs_days = collections.Counter(o["observed_at"][:10] for o in observations)
    change_days = collections.Counter(c["changed_at"][:10] for c in changes)
    recent = sorted(first_days)[-8:-1]  # whole days only, the newest is partial
    new_per_day = statistics.median([first_days[d] for d in recent]) if recent else 0
    obs_recent = sorted(obs_days)[-8:-1]
    obs_per_day = statistics.median([obs_days[d] for d in obs_recent]) if obs_recent else 0

    main = data_dir / "listings.csv"
    listing_bytes = sum(p.stat().st_size for p in storage.listing_files(main))
    bytes_per_row = listing_bytes / max(len(listings), 1)
    obs_bytes = dir_bytes(data_dir / "observations")
    obs_bytes_per_row = obs_bytes / max(len(observations), 1)

    raw_per_day = {}
    for source_dir in (data_dir / "raw").glob("*"):
        for day_dir in source_dir.glob("*"):
            raw_per_day[day_dir.name] = raw_per_day.get(day_dir.name, 0) + dir_bytes(day_dir)
    raw_recent = sorted(raw_per_day)[-8:-1]
    raw_bytes_per_day = statistics.median([raw_per_day[d] for d in raw_recent]) if raw_recent else 0

    # What every run pays today to hold every listing ever seen, measured in
    # a fresh interpreter so nothing else this tool loaded is counted.
    load_seconds, rss_mb = measure_listing_load(main)

    days = 365 * YEARS_AHEAD
    rows_then = len(listings) + new_per_day * days
    scale = rows_then / max(len(listings), 1)
    out = {
        "rows_now": len(listings),
        "new_listings_per_day_median": new_per_day,
        "observations_per_day_median": obs_per_day,
        "changes_per_day": dict(sorted(change_days.items())[-8:]),
        "listing_bytes_per_row": round(bytes_per_row),
        "sizes_now_mb": {
            "listings_all_files": round(listing_bytes / 1e6, 1),
            "observations": round(obs_bytes / 1e6, 1),
            "changes": round(dir_bytes(data_dir / "changes") / 1e6, 1),
            "raw": round(dir_bytes(data_dir / "raw") / 1e6, 1),
            "csv": round(dir_bytes(data_dir / "csv") / 1e6, 1),
            "state": round(dir_bytes(data_dir / "state") / 1e6, 1),
            "logs": round(dir_bytes(logs_dir) / 1e6, 1),
        },
        "raw_bytes_per_day_median": raw_bytes_per_day,
        "load_all_listings_now": {"seconds": load_seconds, "peak_python_mb": rss_mb},
        f"in_{YEARS_AHEAD}_years": {
            "listing_rows": round(rows_then),
            "listing_bytes_mb": round(rows_then * bytes_per_row / 1e6),
            "observation_rows": round(len(observations) + obs_per_day * days),
            "observation_mb": round((len(observations) + obs_per_day * days) * obs_bytes_per_row / 1e6),
            "raw_mb_uncompressed_on_disk": round((dir_bytes(data_dir / "raw") + raw_bytes_per_day * days) / 1e6),
            "load_all_listings_seconds_linear": round(load_seconds * scale, 1) if load_seconds else None,
            "peak_python_mb_linear_estimate": round(rss_mb * scale) if rss_mb else None,
            "assumption": "today's daily rates held constant; the market's own "
                          "size does not grow, only the history does",
        },
    }
    if repo is not None:
        out["git"] = git_facts(repo)
    return out


def measure_listing_load(main: Path) -> tuple:
    """(seconds, MB of memory) to load every listing, in a clean process.

    Memory by tracemalloc, not ru_maxrss: Linux carries the peak RSS of the
    process that spawned the probe across exec, so the probe would report
    this tool's own peak rather than the load's."""
    probe = (
        "import sys, time, tracemalloc\n"
        "from common import storage\n"
        "tracemalloc.start()\n"
        "t = time.perf_counter()\n"
        "rows = storage.read_listings(sys.argv[1])\n"
        "print(time.perf_counter() - t, tracemalloc.get_traced_memory()[1] / 1e6)\n"
    )
    result = subprocess.run([sys.executable, "-c", probe, str(main)], cwd=str(REPO_ROOT),
                            capture_output=True, text=True)
    try:
        seconds, mb = result.stdout.split()
        return round(float(seconds), 2), round(float(mb))
    except ValueError:
        return None, None


def git_facts(repo: Path) -> dict:
    def git(*args):
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else ""
    counts = {}
    for line in git("count-objects", "-v").splitlines():
        key, _, value = line.partition(":")
        counts[key.strip()] = value.strip()
    commits_per_day = collections.Counter(git("log", "--format=%ad", "--date=short", "--since=14 days ago").split())
    return {
        "count_objects": counts,
        "commits_per_day": dict(sorted(commits_per_day.items())),
        "shallow": (repo / ".git" / "shallow").exists(),
    }


# --- GitHub Actions --------------------------------------------------------------


def actions(repos: Iterable[str], since: str, token: str) -> dict:
    out = {}
    for repo in repos:
        runs, page = [], 1
        while page <= 10:
            url = (f"https://api.github.com/repos/{repo}/actions/runs"
                   f"?per_page=100&page={page}&created=%3E%3D{since}")
            request = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(request, timeout=60) as response:
                batch = json.load(response).get("workflow_runs", [])
            runs.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        per_workflow = collections.defaultdict(lambda: {"conclusions": collections.Counter(),
                                                        "queue_min": [], "run_min": []})
        for run in runs:
            entry = per_workflow[run["name"]]
            entry["conclusions"][run.get("conclusion") or run.get("status")] += 1
            created = storage_time(run.get("created_at", ""))
            started = storage_time(run.get("run_started_at", ""))
            updated = storage_time(run.get("updated_at", ""))
            if created and started:
                entry["queue_min"].append((started - created).total_seconds() / 60)
            if started and updated and run.get("status") == "completed":
                entry["run_min"].append((updated - started).total_seconds() / 60)
        out[repo] = {
            "runs": len(runs),
            "failed": [{"id": r["id"], "name": r["name"], "created_at": r["created_at"]}
                       for r in runs if r.get("conclusion") in ("failure", "timed_out", "startup_failure")],
            "per_workflow": {name: {"conclusions": dict(v["conclusions"]),
                                    "queue_minutes": quantiles(v["queue_min"], (0.5, 0.9, 1.0)),
                                    "run_minutes": quantiles(v["run_min"], (0.5, 0.9, 1.0)),
                                    "run_minutes_total": round(sum(v["run_min"]))}
                             for name, v in sorted(per_workflow.items())},
        }
    return out


# --- output ----------------------------------------------------------------------


def to_markdown(facts: dict) -> str:
    lines = [f"# Inventura - fakta ({facts['generated_at']})", ""]
    for section, content in facts.items():
        if section == "generated_at":
            continue
        lines += [f"## {section}", "", "```json",
                  json.dumps(content, ensure_ascii=False, indent=1, default=str)[:60_000],
                  "```", ""]
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--logs-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo", help="the data repository's checkout, for git facts")
    parser.add_argument("--github", action="store_true",
                        help="also read Actions history (needs GITHUB_TOKEN)")
    parser.add_argument("--since", default=None, help="Actions runs since (ISO), default 7 days")
    parser.add_argument("--only", nargs="*", help="run only these sections")
    args = parser.parse_args(argv)

    out = Path(args.out).resolve()
    if out == REPO_ROOT or REPO_ROOT in out.parents:
        print("Refusing to write inside the public code repository: the "
              "inventory describes the private dataset.", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    data_dir, logs_dir = Path(args.data_dir), Path(args.logs_dir)
    wanted = set(args.only or [])

    def want(name):
        return not wanted or name in wanted

    listings = storage.read_listings(data_dir / "listings.csv")
    observations = load_observations(data_dir) if (want("lifecycle") or want("growth")) else []
    facts: dict = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    sections = [
        ("listings", lambda: listings_profile(listings, data_dir)),
        ("lifecycle", lambda: lifecycle(listings, observations)),
        ("runs", lambda: runs_profile(load_runs(logs_dir))),
        ("prices", lambda: prices(listings, data_dir)),
        ("series", lambda: series_quality(data_dir)),
        ("raw", lambda: raw_fields(data_dir)),
        ("growth", lambda: growth(listings, observations, load_changes(data_dir), data_dir,
                                  logs_dir, Path(args.repo) if args.repo else None)),
    ]
    for name, build in sections:
        if want(name):
            started = time.perf_counter()
            facts[name] = build()
            print(f"[inventory] {name}: {time.perf_counter() - started:.1f}s", file=sys.stderr)
    if args.github and want("actions"):
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            print("[inventory] --github needs GITHUB_TOKEN", file=sys.stderr)
        else:
            since = args.since or (datetime.now(timezone.utc).date().fromordinal(
                datetime.now(timezone.utc).date().toordinal() - 7).isoformat())
            facts["actions"] = actions(
                ["ZMAN-91/prague-reality-scraper", "ZMAN-91/prague_reality_sector_analysis"],
                since, token)

    (out / "inventory.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (out / "inventory.md").write_text(to_markdown(facts), encoding="utf-8")
    print(f"[inventory] wrote {out / 'inventory.json'} and inventory.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
