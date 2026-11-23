"""The daily report: today, the last week, the last month, and the trend.

    python -m tools.report      -> REPORT.md   (at the data repository's root)

trh_denne.csv is the record; nobody reads a record. This is the page you look
at for ten seconds and know whether anything moved.

THREE COLUMNS, NOT THREE REPORTS

Every indicator is shown over one day, seven days and thirty days side by
side, because the three answer different questions and disagreeing with each
other is the interesting case: arrivals up today but flat over the month is
noise, up over both is a trend.

THE CHARTS

Mermaid, so GitHub renders them where the file lives, and so they cost
nothing but text in a repository meant to last years. They plot the 30-day
window, which is the one worth watching move; a chart of daily values in a
market that turns over in months is a picture of noise. They grow with the
series - a week in they are stubs, a year in they are the point of the file.

WHAT THE REPORT REFUSES TO DO

Say whether a move is good. That depends on which side of the market you are
on, and a report that quietly takes a side is worse than one that takes none.
It marks direction and leaves it there.

State a number it cannot support. Durations are censored on the left - nothing
can be older than the dataset - and departures on the right, since a departure
takes a week to confirm. Both are disclosed where they bite rather than
quietly rendered as fact.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from common import cas, storage
from tools import market

WINDOWS = market.WINDOWS
# The comparison horizons of the state table: a week, a month, a quarter and
# a year back (90 and 365 since 2026-11-23).
HORIZONS = (7, 30, 90, 365)
WINDOW_HEADS = {1: "poslední den", 7: "7 dnů", 30: "30 dnů", 90: "90 dnů",
                365: "365 dnů"}
HORIZON_HEADS = {7: "před 7 dny", 30: "před 30 dny", 90: "před 90 dny",
                 365: "před rokem"}

# The window the flow charts are drawn from. Thirty days of a market that
# turns over in months is the shortest line that means anything.
CHART_WINDOW = 30
# Points per chart. Mermaid prints every x label, so a daily chart over the
# whole series became an unreadable smear of 60 labels by November; past this
# the series is drawn a point per week, then per month, then per quarter.
CHART_MAX_POINTS = 16

# The first day on which every source was collected across the whole city.
# Before it the collection was still being widened - iDNES to all of Prague
# on 21 September, sreality on 23 September - and supply rose from 4 636 to
# 7 262 flats for sale in a week without the market doing anything. Any
# comparison or flow that reaches back before this day measures the
# collection growing, not the market: the W40 report counted 8 577 "new"
# flats for sale in 30 days against a stock of 7 308. Such cells are shown
# as "—" with the reason; the 30-day window becomes comparable on its own
# on 2026-10-23.
COMPARABLE_FROM = date(2026, 9, 24)

# A fall in asking price beyond this is read as a corrected price, not a
# discount (see notable_tables).
CORRECTION_CUT_PCT = 50.0

# Levels that are floors while the history is short: nothing can be older
# than the dataset, so these rise every day by construction. An arrow on
# them reports the calendar, not the market.
CENSORED_LEVELS = {"stari_median_dnu"}

# (column, label, unit, which windows it makes sense in)
# A level is the same number whichever window you ask for, so it is shown once.
LEVELS = [
    ("nabidka", "Nabídka", "ks"),
    ("cena_median", "Medián ceny", "Kč"),
    ("cena_prumer", "Průměr ceny", "Kč"),
    ("cena_m2_median", "Medián Kč/m²", "Kč"),
    ("cena_m2_prumer", "Průměr Kč/m²", "Kč"),
    ("stari_median_dnu", "Medián stáří nabídky", "dnů"),
]
FLOWS = [
    ("nove", "Nové", "ks"),
    ("nove_denne", "Nové za den", ""),
    ("zmizele", "Zmizelé", "ks"),
    ("zmizele_denne", "Zmizelé za den", ""),
    ("nabidka_prumer", "Průměrná nabídka", "ks"),
    ("absorpce_pct", "Absorpce / 30 dnů", "%"),
    ("mesicu_zasoby", "Měsíců zásoby", ""),
    ("cena_zmizelych_median", "Medián ceny zmizelých", "Kč"),
    ("cena_rychlych_median", "Medián ceny rychle zmizelých", "Kč"),
    ("dnu_na_trhu_median", "Medián dnů na trhu", "dnů"),
    ("dnu_na_trhu_prumer", "Průměr dnů na trhu", "dnů"),
    ("zlevnilo", "Zlevnilo", "ks"),
    ("zlevnilo_pct", "Podíl zlevněných", "%"),
    ("zlevneni_prumer", "Zlevnění na nemovitost", ""),
    ("sleva_median_pct", "Medián slevy", "%"),
    ("sleva_prumer_pct", "Průměr slevy", "%"),
    ("upravilo", "Upravilo (mimo cenu)", "ks"),
    ("upravilo_pct", "Podíl upravených (mimo cenu)", "%"),
]

# What is worth a picture. Four, deliberately: a page of charts is a page
# nobody reads.
# (column, title, window): levels from the one-day rows, flows from the
# thirty-day window - and those only from the day the window is full of
# comparable days, or the line climbs while the window fills: the share of
# discounted listings "rose" from 7 % to 20 % in October because the window
# was filling, not because sellers were cutting.
CHARTS = [
    ("nabidka", "Nabídka", 1),
    ("cena_m2_median", "Medián Kč/m²", 1),
    ("mesicu_zasoby", "Měsíců zásoby (okno 30 dnů)", CHART_WINDOW),
    ("zlevnilo_pct", "Podíl zlevněných za 30 dnů (%)", CHART_WINDOW),
]


def as_num(value):
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def price_move(discount_pct) -> str:
    """A price move from the price's point of view, signed.

    `discount_pct` is positive when the asking price has come down, so a
    property that went UP carries a negative discount - and the first real
    report printed "-0.45 %" in a column headed "sleva", which says the
    opposite of what happened. Negating it once here means the column can
    say what it means: minus is cheaper, plus is dearer.
    """
    value = as_num(discount_pct)
    if value is None:
        return "—"
    return f"{-value:+.2f} %"


def dny(n: int) -> str:
    """Czech agreement, because "řada pokrývá 1 dnů" reads like a bug report."""
    n = int(n)
    if n == 1:
        return "1 den"
    if 2 <= n <= 4:
        return f"{n} dny"
    return f"{n} dnů"


def fmt(value, unit: str) -> str:
    if value is None:
        return "—"
    if unit in ("Kč", "ks", "dnů"):
        return f"{value:,.0f}".replace(",", " ")
    if unit == "%":
        return f"{value:.2f} %"
    return f"{value:.2f}"


def arrow(now, then) -> str:
    """Direction only, never judgement."""
    if now is None or then is None or then == 0:
        return ""
    change = 100.0 * (now - then) / then
    if abs(change) < 1:
        return "→"
    return f"↑{change:+.0f} %" if change > 0 else f"↓{change:+.0f} %"


def rows_for(series: list[dict], segment: str, window: int) -> dict[str, dict]:
    return {r["den"]: r for r in series
            if r["segment"] == segment and int(r["okno_dnu"] or 0) == window}


def value_on_or_before(by_day: dict[str, dict], when: date, field: str):
    """As of `when`, or the closest earlier day that has it. Never later -
    borrowing a reading from after the horizon reports a change over a
    shorter window than the column claims."""
    for offset in range(0, 8):
        row = by_day.get((when - timedelta(days=offset)).isoformat())
        if row is not None and row.get("den_sebran") != "ne":
            got = as_num(row.get(field))
            if got is not None:
                return got
    return None


def bucketed(points: list[tuple[str, float]],
             max_points: int = CHART_MAX_POINTS) -> tuple[list[tuple[str, float]], str]:
    """Thin a daily series to at most `max_points` readable points.

    A day per point while that fits, then the last day of each week, month
    or quarter - the value the period ended on, not an average, so a level
    reads the same as in the tables. Returns the points with their labels
    and the name of the step, for the caption.
    """
    if len(points) <= max_points:
        return [(f"{int(d[8:])}.{int(d[5:7])}.", v) for d, v in points], "den"

    def period(d: str, step: str) -> str:
        day = date.fromisoformat(d)
        if step == "týden":
            year, week, _ = day.isocalendar()
            return f"{year}-{week:02d}"
        if step == "měsíc":
            return d[:7]
        return f"{day.year}-Q{(day.month - 1) // 3 + 1}"

    for step in ("týden", "měsíc", "čtvrtletí"):
        last: dict[str, tuple[str, float]] = {}
        for d, v in points:
            last[period(d, step)] = (d, v)
        kept = [last[k] for k in sorted(last)]
        if len(kept) <= max_points or step == "čtvrtletí":
            break

    def label(d: str) -> str:
        day = date.fromisoformat(d)
        if step == "týden":
            return f"{day.day}.{day.month}.{day.year % 100:02d}"
        if step == "měsíc":
            return f"{day.month}/{day.year % 100:02d}"
        return f"Q{(day.month - 1) // 3 + 1}/{day.year % 100:02d}"

    return [(label(d), v) for d, v in kept], step


def chart(by_day: dict[str, dict], field: str, label: str) -> list[str]:
    """A Mermaid line chart of one indicator over the whole series.

    Days without the night pass are left out: on them departures and
    arrivals had not been seen yet, and the line dipped by hundreds of flats
    that never left. Returns [] when there is not enough of a line to be
    worth drawing: two points joined up look like a trend and are not one.
    """
    days = sorted(by_day)
    points = [(d, as_num(by_day[d].get(field))) for d in days
              if by_day[d].get("den_sebran") != "ne"]
    points = [(d, v) for d, v in points if v is not None]
    if len(points) < 3:
        return []

    points, _ = bucketed(points)
    labels = ", ".join(f'"{d}"' for d, _ in points)
    values = ", ".join(f"{v:.2f}" for _, v in points)
    low = min(v for _, v in points)
    high = max(v for _, v in points)
    pad = max((high - low) * 0.1, abs(high) * 0.01, 1)
    # Nothing this reports can be negative - a count of flats, a price, a
    # share, a number of months of stock. Padding below zero drew the supply
    # axis from -270 to 3623 and spent the bottom of the picture on values
    # that cannot occur.
    bottom = max(0.0, low - pad) if low >= 0 else low - pad

    return [
        "```mermaid",
        "xychart-beta",
        f'    title "{label}"',
        f"    x-axis [{labels}]",
        f'    y-axis "{label}" {bottom:.2f} --> {high + pad:.2f}',
        f"    line [{values}]",
        "```",
        "",
    ]


def caveats(latest_row: dict, partial: bool = False,
            comparable_from: date = COMPARABLE_FROM) -> list[str]:
    """The things the numbers cannot say for themselves."""
    out = []
    if partial:
        out.append(
            "- **Den ještě neskončil.** Není tu zatím ani jeden celý den, "
            "takže nabídka je spodní odhad: nemovitost, kterou dnešní sběry "
            "ještě nestihly projít, v ní chybí. Doplní se sama, jak den běží."
        )
    history = as_num(latest_row.get("uplnost_dnu")) or 0
    if history < 90:
        out.append(
            f"- **Historie je {dny(history)}.** Nic nemůže být starší než "
            "dataset, takže `dnu_na_trhu` a `stari_median_dnu` jsou zatím "
            "**spodní meze, ne měření** — skutečná čísla mohou být jen vyšší. "
            "Srovnatelné budou, až historie přesáhne typickou dobu prodeje, "
            "tedy měsíce."
        )
    latest = latest_row.get("den")
    if latest and date.fromisoformat(latest) - timedelta(days=max(WINDOWS) - 1) < comparable_from:
        out.append(
            f"- **„—“ znamená, že okno sahá před {comparable_from:%-d. %-m. %Y}.** Do té doby "
            "se sběr rozšiřoval (iDNES na celou Prahu 21. 9., sreality 23. 9.), "
            "takže nabídka i nové v něm rostou se sběrem, ne s trhem. Grafy "
            "proto začínají až tímto dnem."
        )
    out.append(
        f"- **Posledních {dny(market.CONFIRMATION_LAG_DAYS)} podhodnocuje odchody.** "
        f"Zmizení se potvrzuje až po {market.CONFIRMATION_LAG_DAYS} dnech nepřítomnosti, takže co odešlo "
        "včera, ještě čeká ve stavu `missing`. Sloupec `zmizele_potvrzeno` "
        "v datech označuje dny, kde se to už usadilo."
    )
    return out


def gap_ranges(series: list[dict]) -> list[tuple[date, date]]:
    """Runs of days without the night pass, as (first, last)."""
    gaps = sorted({date.fromisoformat(r["den"]) for r in series
                   if r.get("den_sebran") == "ne"})
    out: list[tuple[date, date]] = []
    for day in gaps:
        if out and (day - out[-1][1]).days == 1:
            out[-1] = (out[-1][0], day)
        else:
            out.append((day, day))
    return out


def span_text(first: date, last: date) -> str:
    if first == last:
        return f"{first.day}. {first.month}."
    return f"{first.day}. {first.month}.–{last.day}. {last.month}."


def render(series: list[dict], episodes_rows: list[dict],
           generated: Optional[datetime] = None,
           comparable_from: date = COMPARABLE_FROM,
           dashboard_link: Optional[str] = None) -> str:
    generated = generated or datetime.now(timezone.utc)
    if not series:
        return "# Report trhu\n\nZatím nejsou žádná data.\n"

    days = sorted({r["den"] for r in series})
    # The day in progress is not a day yet. An episode leaves the live set on
    # its last_seen, so one not yet swept today looks like it ended yesterday:
    # at 02:15 on the first full day supply read 4402 against 4721 episodes,
    # a 7% undercount that fills itself in as the day's sweeps land. Reporting
    # it as "today's market" would be reporting how far through the day the
    # collection had got.
    complete = [r["den"] for r in series if r.get("den_uplny") == "ano"]
    latest = max(complete) if complete else days[-1]
    partial = not complete
    segments = sorted({r["segment"] for r in series},
                      key=lambda s: (not s.startswith("vse/"), s))

    out = [
        "# Report trhu",
        "",
        f"Data k **{latest}** · vygenerováno {generated:%Y-%m-%d %H:%M} UTC · "
        f"řada pokrývá {dny(len(days))}",
        "",
        "Šipka je směr, ne hodnocení — jestli je růst dobrá zpráva, záleží na "
        "tom, na které straně trhu stojíš.",
        "",
    ]
    if dashboard_link:
        out += [f"**[Interaktivní grafy]({dashboard_link})** — libovolný ukazatel, "
                "segment a období, celá historie.", ""]
    gaps = [g for g in gap_ranges(series) if g[0].isoformat() <= latest]
    if gaps:
        out += [
            "**Dny bez celoměstského sběru: "
            + ", ".join(span_text(a, b) for a, b in gaps)
            + f" {gaps[-1][1].year}.** Zmizení a nové inzeráty z těch dnů se "
            "zapsaly až první den po nich; grafy tyto dny vynechávají a "
            "srovnání s nimi berou nejbližší předchozí sebraný den.",
            "",
        ]

    for segment in segments:
        windows = {w: rows_for(series, segment, w) for w in WINDOWS}
        today = windows[1].get(latest)
        if today is None:
            continue

        out += [f"## {segment}", ""]
        out += caveats(today, partial, comparable_from) + [""]

        out += ["### Stav", "",
                "| ukazatel | teď | " + " | ".join(HORIZON_HEADS[h] for h in HORIZONS) + " |",
                "|---|---:|" + "---:|" * len(HORIZONS)]
        by_day = windows[1]
        censored = (as_num(today.get("uplnost_dnu")) or 0) < 90
        for field, label, unit in LEVELS:
            now = as_num(today.get(field))
            cells = [f"**{fmt(now, unit)}**"]
            for horizon in HORIZONS:
                when = date.fromisoformat(latest) - timedelta(days=horizon)
                if when < comparable_from:
                    cells.append("—")
                    continue
                then = value_on_or_before(by_day, when, field)
                shown = "" if field in CENSORED_LEVELS and censored else arrow(now, then)
                cells.append(f"{fmt(then, unit)} {shown}".strip())
            out.append(f"| {label} | " + " | ".join(cells) + " |")
        out.append("")

        out += ["### Tok", "",
                "| ukazatel | " + " | ".join(WINDOW_HEADS[w] for w in WINDOWS) + " |",
                "|---|" + "---:|" * len(WINDOWS)]
        for field, label, unit in FLOWS:
            cells = []
            for window in WINDOWS:
                starts = date.fromisoformat(latest) - timedelta(days=window - 1)
                if starts < comparable_from:
                    cells.append("—")
                    continue
                row = windows[window].get(latest) or {}
                cells.append(fmt(as_num(row.get(field)), unit))
            out.append(f"| {label} | " + " | ".join(cells) + " |")
        out.append("")

        drawn = []
        steps = set()
        for field, label, window in CHARTS:
            full_from = comparable_from + timedelta(days=window - 1)
            usable = {d: r for d, r in windows[window].items()
                      if date.fromisoformat(d) >= full_from and d <= latest}
            lines = chart(usable, field, label)
            if lines:
                drawn += lines
                points = [d for d, r in usable.items()
                          if r.get("den_sebran") != "ne" and as_num(r.get(field)) is not None]
                steps.add(bucketed([(d, 0.0) for d in sorted(points)])[1])
        if drawn:
            step = {"den": "den", "týden": "týden (hodnota posledního dne týdne)",
                    "měsíc": "měsíc (hodnota posledního dne)",
                    "čtvrtletí": "čtvrtletí (hodnota posledního dne)"}
            out += ["### Vývoj", "",
                    f"Celá řada od {comparable_from.day}. {comparable_from.month}. "
                    f"{comparable_from.year}; jeden bod = "
                    + " / ".join(step[s] for s in sorted(steps)) + ".", ""]
            out += drawn
        else:
            out += ["### Vývoj", "",
                    "_Zatím příliš krátká řada na graf — tři body nejsou trend._",
                    ""]

    out += ["## Za pozornost", ""] + notable_tables(episodes_rows)
    out += [
        "---",
        "",
        "Zmizení neznamená prodej — žádný portál to nezveřejňuje. Nejbližší "
        "dostupný náhradník je *rychle zmizelé*: kdo nabídku vzdává, málokdy "
        "to udělá do dvou týdnů.",
        "",
        "Zdroj: `data/csv/trh_denne.csv`, `data/csv/historie_nemovitosti.csv`.",
        "",
    ]
    return "\n".join(out)


def _where(row: dict) -> str:
    """A readable address from the parsed columns.

    The raw string the portal published is not stored any more (see
    schema.LISTING_FIELDS), and it was never the better label anyway: it was
    whichever shape that portal happened to use, so a column of them read as
    three different conventions stacked on top of each other.
    """
    street = (row.get("ulice") or "").strip()
    number = (row.get("cislo_popisne") or "").strip()
    district = (row.get("mestska_cast") or "").strip()
    head = f"{street} {number}".strip() if street else ""
    return ", ".join(part for part in (head, district) if part) or "?"


def notable_tables(episodes_rows: list[dict], limit: int = 5) -> list[str]:
    live = [r for r in episodes_rows if r.get("outcome") == "active"]
    gone = [r for r in episodes_rows if r.get("outcome") == "removed"]

    def top(rows, key, reverse=True, positive=True):
        """The top of a descending ranking must be something that happened.

        Ranked by discount, the first clean run listed five properties at
        0.00 % - and the same five again under "most edited" at zero edits,
        because with every value tied the sort just returned file order.
        A zero is not a small discount, it is the absence of one.
        """
        kept = [r for r in rows if as_num(r.get(key)) is not None]
        if positive:
            kept = [r for r in kept if as_num(r.get(key)) > 0]
        kept.sort(key=lambda r: as_num(r.get(key)), reverse=reverse)
        return kept[:limit]

    # A cut of more than half is almost never a discount. The first clean W40
    # table was led by -91 %, -67 % and -56 %: a price typed with an extra
    # digit and then corrected, a listing re-used for a smaller unit. Ranked
    # by depth, corrections always outrank every real discount, so they are
    # counted and left out of the ranking instead.
    corrections = [r for r in live
                   if (as_num(r.get("discount_pct")) or 0) > CORRECTION_CUT_PCT]
    discounts = [r for r in live
                 if (as_num(r.get("discount_pct")) or 0) <= CORRECTION_CUT_PCT]
    groups = [
        ("Největší slevy", top(discounts, "discount_pct")),
        ("Nejdéle na trhu", top(live, "days_on_market")),
        # Gone on the day it appeared is the fastest departure there is, so
        # zero belongs in this one.
        ("Nejrychleji zmizelé",
         top(gone, "days_on_market", reverse=False, positive=False)),
        ("Nejvíc upravované", top(live, "attribute_changes")),
    ]

    out = []
    for title, rows in groups:
        out.append(f"### {title}")
        if title == "Největší slevy" and corrections:
            out += ["", f"_Vynecháno poklesů o víc než {CORRECTION_CUT_PCT:.0f} %: "
                        f"{len(corrections)} — skoro vždy jde o opravu překlepu "
                        "v ceně, ne o slevu._"]
        if not rows:
            out += ["", "_nic_", ""]
            continue
        if title == "Nejdéle na trhu":
            oldest = max((as_num(r.get("days_on_market")) or 0) for r in live)
            tied = sum(1 for r in live if (as_num(r.get("days_on_market")) or 0) >= oldest)
            if tied > limit:
                # W47 listed five at 67 days - the age of the dataset, not of
                # the listings. Which five of the tied was file order.
                out += ["", f"_V nabídce od začátku sběru ({int(oldest)} dnů) je "
                            f"{tied} inzerátů; jak dlouho doopravdy, nevíme. "
                            "Tabulka ukazuje jen pět z nich._"]
        out += ["", "| adresa | dispozice | cena | dnů | změna ceny | úprav |",
                "|---|---|---:|---:|---:|---:|"]
        for r in rows:
            out.append(
                f"| {_where(r)[:40]} "
                f"| {r.get('disposition') or '?'} "
                f"| {fmt(as_num(r.get('last_price')), 'Kč')} "
                f"| {r.get('days_on_market')} "
                f"| {price_move(r.get('discount_pct'))} "
                f"| {r.get('attribute_changes') or 0} |")
        out.append("")
    return out


def export(data_dir: Path = storage.DATA_DIR,
           generated: Optional[datetime] = None,
           through: Optional[date] = None) -> dict:
    daily_path = data_dir / "csv" / "trh_denne.csv"
    if not daily_path.exists():
        market.export(data_dir)
    with open(daily_path, encoding="utf-8", newline="") as f:
        series = list(csv.DictReader(f))
    with open(data_dir / "csv" / "historie_nemovitosti.csv",
              encoding="utf-8", newline="") as f:
        episodes_rows = list(csv.DictReader(f))

    # The report is about a WEEK, not about everything up to yesterday, and
    # the two only differ when it runs late. Monday they are the same; on the
    # Tuesday retry "up to yesterday" quietly takes in Monday, so that week
    # is a day long and the next one a day short - two wrong weeks, not one.
    #
    # So the series is cut at the last Sunday that has actually passed, and
    # both Monday and its Tuesday retry produce the same report.
    through = through or cas.closed_week_end(cas.to_prague(
        generated).date() if generated else None)
    series = [row for row in series if (row.get("den") or "") <= through.isoformat()]

    root = data_dir.parent

    # The interactive charts, rebuilt with the report so the two agree. Where
    # they are published (a private page) is kept in reports/dashboard_url.txt
    # in the data repository; without it the report links the file itself,
    # which opens when the repository is on disk.
    from tools import dashboard
    dashboard.export(data_dir, generated, through)
    url_file = root / "reports" / "dashboard_url.txt"
    url = url_file.read_text(encoding="utf-8").strip() if url_file.exists() else ""

    text = render(series, episodes_rows, generated,
                  dashboard_link=url or "reports/trh.html")
    path = root / "REPORT.md"
    path.write_text(text, encoding="utf-8")
    if not url:
        text = render(series, episodes_rows, generated, dashboard_link="trh.html")

    # And a copy that nothing ever overwrites. REPORT.md is the current
    # week's, by definition, so every week it replaces the one before it -
    # and the only trace of what last week said was a commit in the data
    # repository's history. That is recoverable but not browsable, and it is
    # not in the weekly archive at all: restore the archive into an empty
    # tree and every report ever written is gone.
    #
    # Named for the week it is ABOUT. reports/2026-W38.md is the report on
    # the week to Sunday 20 September, whether it was rendered on the Monday
    # or on the Tuesday after a failure - which is what makes the retry
    # produce the same file rather than a second one beside it.
    archive = root / "reports"
    archive.mkdir(parents=True, exist_ok=True)
    weekly = archive / f"{cas.week_label(through)}.md"
    weekly.write_text(text, encoding="utf-8")

    return {"path": str(path), "archived": str(weekly),
            "days": len({r["den"] for r in series}),
            "segments": len({r["segment"] for r in series})}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    args = parser.parse_args()
    stats = export(Path(args.data_dir))
    print(f"[report] {stats['path']}: {stats['days']} days, "
          f"{stats['segments']} segments (kept as {stats['archived']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
