"""Interactive charts of the market series: reports/trh.html.

    python -m tools.dashboard --data-dir store/data
    python -m tools.dashboard --data-dir store/data --fragment out.html

The weekly report's Mermaid charts are fixed pictures: one window, one
segment each, four indicators. This page holds every indicator of
trh_denne.csv for every segment and window over the whole series, and lets
the reader pick - which indicator, which segments side by side, which window,
which period, smoothed or not, as values or as an index of the period's
start. Asked for on 2026-11-23, with the report linking to it.

One self-contained file: the data is embedded, so it opens from a disk as
well as from anywhere it is published. The chart library (uPlot) comes from
cdn.jsdelivr.net, so it needs a connection. The page itself lives in
tools/dashboard_template.html; this only fills in the data. It is written to
the data repository, never to this one - the numbers are private.

`--fragment` writes the page without its document skeleton, for publishing
somewhere that supplies its own.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from common import cas, storage
from tools import market, report

TEMPLATE = Path(__file__).with_name("dashboard_template.html")

# What the page offers, in the report's own words. Levels are a day's state
# and the same in every window; flows are counted over a window.
METRICS = ([(f, label, unit, "level") for f, label, unit in report.LEVELS]
           + [(f, label, unit, "flow") for f, label, unit in report.FLOWS])

SKELETON_HEAD = ('<!doctype html>\n<html lang="cs">\n<head>\n<meta charset="utf-8">\n'
                 '<meta name="viewport" content="width=device-width, initial-scale=1, '
                 'viewport-fit=cover">\n</head>\n<body>\n')
SKELETON_TAIL = "\n</body>\n</html>\n"


def as_num(value):
    if value in ("", None):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 2)


def build_data(series: list[dict], generated: Optional[datetime] = None,
               comparable_from: date = report.COMPARABLE_FROM) -> dict:
    """The embedded data: one array per (segment, window, indicator), aligned
    to `days`. Only days that are over - the day in progress is a partial
    count (see report.render)."""
    generated = generated or datetime.now(timezone.utc)
    complete = [r for r in series if r.get("den_uplny") == "ano"]
    rows = complete or series
    days = sorted({r["den"] for r in rows})
    index = {d: i for i, d in enumerate(days)}
    segments = sorted({r["segment"] for r in rows},
                      key=lambda s: (s.split("/")[1], not s.startswith("vse/"), s))
    windows = sorted({int(r["okno_dnu"]) for r in rows})

    data: dict[str, list] = {}
    for r in rows:
        window = int(r["okno_dnu"])
        for field, _, _, kind in METRICS:
            if kind == "level" and window != 1:
                continue
            key = f"{r['segment']}|{window}|{field}"
            if key not in data:
                data[key] = [None] * len(days)
            data[key][index[r["den"]]] = as_num(r.get(field))
    # Arrays that never got a value are left out; the page shows "no data".
    data = {k: v for k, v in data.items() if any(x is not None for x in v)}

    return {
        "generated": f"{generated:%Y-%m-%d %H:%M} UTC",
        "latest": days[-1] if days else "",
        "comparable_from": (comparable_from.isoformat()
                            if days and comparable_from.isoformat() > days[0] else ""),
        "days": days,
        "gaps": sorted({r["den"] for r in rows if r.get("den_sebran") == "ne"}),
        "segments": segments,
        "windows": windows,
        "metrics": [{"key": f, "label": label, "unit": unit, "kind": kind}
                    for f, label, unit, kind in METRICS],
        "series": data,
    }


def render(data: dict, fragment: bool = False) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # Inside a <script> element nothing may close it early.
    payload = payload.replace("</", "<\\/")
    page = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/", payload, 1)
    return page if fragment else SKELETON_HEAD + page + SKELETON_TAIL


def export(data_dir: Path = storage.DATA_DIR, generated: Optional[datetime] = None,
           through: Optional[date] = None, out: Optional[Path] = None,
           fragment: bool = False) -> dict:
    with open(data_dir / "csv" / "trh_denne.csv", encoding="utf-8", newline="") as f:
        series = list(csv.DictReader(f))
    # The same week the report is about, so the two agree.
    if through is not None:
        series = [r for r in series if (r.get("den") or "") <= through.isoformat()]
    data = build_data(series, generated)
    path = out or (data_dir.parent / "reports" / "trh.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(data, fragment), encoding="utf-8")
    return {"path": str(path), "days": len(data["days"]), "series": len(data["series"])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--out", default=None)
    parser.add_argument("--fragment", action="store_true",
                        help="without the document skeleton, for publishing")
    args = parser.parse_args(argv)
    stats = export(Path(args.data_dir), out=Path(args.out) if args.out else None,
                   fragment=args.fragment)
    print(f"[dashboard] {stats['path']}: {stats['days']} days, {stats['series']} series")
    return 0


if __name__ == "__main__":
    sys.exit(main())
