"""A readable report over the daily indicator series.

trh_denne.csv is the record; nobody reads a record. This turns it into
something you can look at in ten seconds and know whether anything moved:
the current level of every indicator, what it was a week and a month ago, and
the handful of individual properties worth a glance.

    python -m tools.report            -> data/REPORT.md

Regenerated on every run, so it is always about the latest data and never
about whenever somebody last remembered to produce it. That is also why it
is a tool and not a one-off: a report you have to remember to make is a
report that stops existing in three weeks.

The comparisons are against fixed horizons rather than against the previous
run. Hour-on-hour movement in a market that turns over in months is noise,
and a report full of noise trains you to ignore it.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from common import storage
from tools import market

# A week catches anything sudden; a month is the shortest horizon on which
# this market says anything at all.
HORIZONS = (7, 30)

# How the indicators read, and which direction is worth noticing. The label
# is what a person reads; `good_up` says whether a rise favours sellers, so
# the report can mark direction without pretending to judge it.
INDICATORS = [
    ("nabidka", "Nabídka", "ks", None),
    ("nove", "Nové za den", "ks", None),
    ("zmizele", "Zmizelé za den", "ks", None),
    ("absorpce_pct", "Absorpce", "%", None),
    ("cena_median", "Medián ceny", "Kč", None),
    ("cena_m2_median", "Medián Kč/m²", "Kč", None),
    ("cena_zmizelych", "Medián ceny zmizelých", "Kč", None),
    ("cena_rychlych", "Medián ceny rychle zmizelých", "Kč", None),
    ("dnu_na_trhu_median", "Medián dnů na trhu", "dnů", None),
    ("zlevnilo_pct", "Podíl zlevněných", "%", None),
    ("zlevneni_prumer", "Průměrný počet zlevnění", "", None),
    ("sleva_median_pct", "Medián hloubky slevy", "%", None),
    ("upravilo_pct", "Podíl upravených (mimo cenu)", "%", None),
    ("uprav_prumer", "Průměrný počet úprav", "", None),
]


def as_num(value):
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt(value, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "Kč":
        return f"{value:,.0f}".replace(",", " ")
    if unit == "%":
        return f"{value:.2f} %"
    if unit == "ks" or unit == "dnů":
        return f"{value:,.0f}".replace(",", " ")
    return f"{value:.2f}"


def arrow(now, then) -> str:
    """Direction only. This file does not decide whether a rise is good news -
    that depends on which side of the market you are on, and a report that
    quietly takes a side is worse than one that takes none."""
    if now is None or then is None or then == 0:
        return ""
    change = 100.0 * (now - then) / then
    if abs(change) < 1:
        return "→"
    return f"↑ {change:+.0f} %" if change > 0 else f"↓ {change:+.0f} %"


def series_by_day(rows: list[dict], segment: str) -> dict[str, dict]:
    return {r["den"]: r for r in rows if r["segment"] == segment}


def value_on_or_before(by_day: dict[str, dict], when: date, field: str):
    """The indicator as of `when`, or the closest earlier day that has it.

    A horizon that lands before the data starts, or on a day an indicator was
    too thin to measure, must read as "no comparison" rather than as a change
    from zero.
    """
    for offset in range(0, 8):
        key = (when - timedelta(days=offset)).isoformat()
        row = by_day.get(key)
        if row is not None:
            got = as_num(row.get(field))
            if got is not None:
                return got
    return None


def notable(episodes_rows: list[dict], limit: int = 5) -> dict:
    """The few individual properties worth a look, which an aggregate hides."""
    live = [r for r in episodes_rows if r.get("outcome") == "active"]
    gone = [r for r in episodes_rows if r.get("outcome") == "removed"]

    def top(rows, key, reverse=True, having=None):
        kept = [r for r in rows if as_num(r.get(key)) is not None
                and (having is None or having(r))]
        kept.sort(key=lambda r: as_num(r.get(key)), reverse=reverse)
        return kept[:limit]

    return {
        "Největší slevy": top(live, "discount_pct"),
        "Nejdéle na trhu": top(live, "days_on_market"),
        "Nejrychleji zmizelé": top(gone, "days_on_market", reverse=False),
        "Nejvíc upravované": top(live, "attribute_changes"),
    }


def render(series: list[dict], episodes_rows: list[dict],
           generated: Optional[datetime] = None) -> str:
    generated = generated or datetime.now(timezone.utc)
    if not series:
        return "# Report trhu\n\nZatím nejsou žádná data.\n"

    days = sorted({r["den"] for r in series})
    latest = date.fromisoformat(days[-1])
    segments = sorted({r["segment"] for r in series},
                      key=lambda s: (not s.startswith("vse/"), s))

    out = [
        "# Report trhu",
        "",
        f"Data k **{latest.isoformat()}**, vygenerováno "
        f"{generated.strftime('%Y-%m-%d %H:%M UTC')}. "
        f"Řada pokrývá {len(days)} dnů.",
        "",
        "Šipka je směr, ne hodnocení — jestli je růst dobrá zpráva, záleží na "
        "tom, na které straně trhu stojíš.",
        "",
    ]

    if len(days) < max(HORIZONS):
        out += [
            f"> Řada je zatím {len(days)} dnů dlouhá, takže srovnání na "
            f"{max(HORIZONS)} dnů ještě nemá o co se opřít a je prázdné. "
            "To není chyba, jen mládí datasetu.",
            "",
        ]

    for segment in segments:
        by_day = series_by_day(series, segment)
        if latest.isoformat() not in by_day:
            continue
        out += [f"## {segment}", "",
                "| ukazatel | teď | " +
                " | ".join(f"před {h} dny" for h in HORIZONS) + " |",
                "|---|---:|" + "---:|" * len(HORIZONS)]
        for field, label, unit, _ in INDICATORS:
            now = as_num(by_day[latest.isoformat()].get(field))
            cells = [f"**{fmt(now, unit)}**"]
            for horizon in HORIZONS:
                then = value_on_or_before(by_day, latest - timedelta(days=horizon), field)
                mark = arrow(now, then)
                cells.append(f"{fmt(then, unit)} {mark}".strip())
            out.append(f"| {label} | " + " | ".join(cells) + " |")
        out.append("")

    out += ["## Za pozornost", ""]
    for title, rows in notable(episodes_rows).items():
        out.append(f"### {title}")
        if not rows:
            out += ["", "_nic_", ""]
            continue
        out += ["", "| adresa | dispozice | cena | dnů | sleva | úprav |",
                "|---|---|---:|---:|---:|---:|"]
        for r in rows:
            out.append(
                f"| {(r.get('address') or '?')[:40]} "
                f"| {r.get('disposition') or '?'} "
                f"| {fmt(as_num(r.get('last_price')), 'Kč')} "
                f"| {r.get('days_on_market')} "
                f"| {fmt(as_num(r.get('discount_pct')), '%')} "
                f"| {r.get('attribute_changes') or 0} |"
            )
        out.append("")

    out += [
        "---",
        "",
        "Zmizení neznamená prodej — žádný portál to nezveřejňuje. "
        "Nejbližší dostupný náhradník je *rychle zmizelé*: kdo nabídku "
        "vzdává, málokdy to udělá do dvou týdnů.",
        "",
        "Zdroj: `data/csv/trh_denne.csv`, `data/csv/historie_nemovitosti.csv`.",
        "",
    ]
    return "\n".join(out)


def export(data_dir: Path = storage.DATA_DIR,
           generated: Optional[datetime] = None) -> dict:
    daily_path = data_dir / "csv" / "trh_denne.csv"
    if not daily_path.exists():
        market.export(data_dir)
    with open(daily_path, encoding="utf-8", newline="") as f:
        series = list(csv.DictReader(f))
    with open(data_dir / "csv" / "historie_nemovitosti.csv",
              encoding="utf-8", newline="") as f:
        episodes_rows = list(csv.DictReader(f))

    text = render(series, episodes_rows, generated)
    path = data_dir.parent / "REPORT.md"
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "days": len({r["den"] for r in series}),
            "segments": len({r["segment"] for r in series})}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    args = parser.parse_args()
    stats = export(Path(args.data_dir))
    print(f"[report] {stats['path']}: {stats['days']} days, "
          f"{stats['segments']} segments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
