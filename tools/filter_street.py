"""Filter the dataset down to one street and save the result separately.

    python -m tools.filter_street "Nurniho"
    python -m tools.filter_street "Nurniho" --include-removed
    python -m tools.filter_street "Roztylska" --property-type byt

Writes data/ulice/<slug>/ containing:
    inzeraty.csv       the matching listings (newest known price folded in)
    cenova_historie.csv every observation ever recorded for those listings
    souhrn.txt          a short human summary

Matching is accent- and case-insensitive and looks at the address field, the
listing URL (sreality bakes the street into its canonical slug) and the
description, because street information reaches the two portals through
different fields - see tools/listing_query.match_street.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

from common import storage
from tools.listing_query import (
    enrich,
    fold,
    is_live,
    load_latest_prices,
    load_listings,
    match_street,
    write_csv,
)
from tools.export_csv import VIEW_FIELDS

OBSERVATION_FIELDS = ["internal_id", "observed_at", "price", "price_per_m2", "status"]


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower() or "ulice"


def price_history(internal_ids: set[str], data_dir: Path) -> list[dict]:
    """Every observation ever recorded for these listings, oldest first."""
    rows: list[dict] = []
    obs_dir = data_dir / "observations"
    if not obs_dir.exists():
        return rows
    for path in sorted(obs_dir.glob("*.csv")):
        with open(path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("internal_id") in internal_ids:
                    rows.append(row)
    rows.sort(key=lambda r: (r.get("internal_id", ""), r.get("observed_at", "")))
    return rows


def filter_street(
    street: str,
    data_dir: Path = storage.DATA_DIR,
    include_removed: bool = False,
    property_type: str | None = None,
    transaction_type: str | None = None,
) -> dict:
    listings = load_listings(data_dir)
    rows = enrich(listings, load_latest_prices(data_dir))

    matched = [r for r in rows if match_street(r, street)]
    if not include_removed:
        matched = [r for r in matched if is_live(r)]
    if property_type:
        matched = [r for r in matched if r.get("property_type") == property_type]
    if transaction_type:
        matched = [r for r in matched if r.get("transaction_type") == transaction_type]

    matched.sort(key=lambda r: (r.get("transaction_type", ""), r.get("address", ""), r.get("internal_id", "")))

    out_dir = data_dir / "ulice" / slugify(street)
    write_csv(matched, out_dir / "inzeraty.csv", VIEW_FIELDS)

    internal_ids = {r["internal_id"] for r in matched}
    history = price_history(internal_ids, data_dir)
    write_csv(history, out_dir / "cenova_historie.csv", OBSERVATION_FIELDS)

    summary = render_summary(street, matched, rows, history)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "souhrn.txt").write_text(summary, encoding="utf-8")

    return {
        "street": street,
        "out_dir": out_dir,
        "matched": len(matched),
        "searched": len(rows),
        "observations": len(history),
        "summary": summary,
    }


def render_summary(street: str, matched: list[dict], all_rows: list[dict], history: list[dict]) -> str:
    lines = [
        f"Ulice: {street}",
        f"Prohledáno inzerátů v datasetu: {len(all_rows)}",
        f"Odpovídajících inzerátů: {len(matched)}",
        f"Záznamů v cenové historii: {len(history)}",
        "",
    ]

    if not matched:
        lines += [
            "Žádný inzerát neodpovídá.",
            "",
            "Možné důvody (v tomto pořadí pravděpodobnosti):",
            "  1) Dataset zatím nepokrývá celý trh (viz odhad pokrytí níže).",
            "  2) Na této ulici teď nic není inzerované - běžný stav u krátké ulice.",
            "  3) Ulice se ve zdrojích píše jinak, než jak byla zadána.",
        ]
        coverage = describe_coverage(all_rows)
        if coverage:
            lines += ["", coverage]
        near = nearest_street_names(street, all_rows)
        if near:
            lines += ["", "Nejpodobnější ulice v datasetu (překlep?):"]
            lines += [f"  - {name}" for name in near]
        return "\n".join(lines) + "\n"

    by_transaction = Counter(r.get("transaction_type", "?") for r in matched)
    by_disposition = Counter(r.get("disposition") or "?" for r in matched)
    lines.append("Podle typu nabídky: " + ", ".join(f"{k}={v}" for k, v in sorted(by_transaction.items())))
    lines.append("Podle dispozice: " + ", ".join(f"{k}={v}" for k, v in sorted(by_disposition.items())))

    prices = [float(r["price"]) for r in matched if r.get("price") not in (None, "")]
    if prices:
        lines.append(
            f"Cena: min {min(prices):,.0f}, medián {sorted(prices)[len(prices) // 2]:,.0f}, "
            f"max {max(prices):,.0f} Kč".replace(",", " ")
        )

    lines += ["", "Inzeráty:"]
    for r in matched:
        price = r.get("price") or "?"
        lines.append(
            f"  [{r.get('transaction_type','?')}/{r.get('property_type','?')}] "
            f"{r.get('disposition') or '?'} {r.get('area_m2') or '?'} m2 - {price} Kč - "
            f"{r.get('address') or '?'} ({r.get('status')})"
        )
        lines.append(f"      {r.get('url')}")
    return "\n".join(lines) + "\n"


def describe_coverage(all_rows: list[dict], logs_dir: Path | None = None) -> str:
    """How much of the market the dataset actually holds, from the run log.

    Without this, a zero-result street lookup reads as "nothing is for sale
    there" when the real answer is often "this project has seen 5% of the
    market so far". The scraper's own run log records what each source
    reported as its total, and how many listings a run had to leave for
    later because it hit its budget - which is exactly the number that makes
    an empty result weak evidence rather than strong.
    """
    logs_dir = logs_dir or (storage.REPO_ROOT / "logs")
    entries: list[dict] = []
    for path in sorted(logs_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    if not entries:
        return f"Dataset obsahuje {len(all_rows)} inzerátů (běhový log není k dispozici)."

    last = entries[-1]
    budget = last.get("budget") or {}
    skipped = budget.get("new_details_skipped_for_budget") or 0
    parts = [
        f"Odhad pokrytí: dataset obsahuje {len(all_rows)} inzerátů "
        f"z {len(entries)} dosavadních běhů.",
    ]
    if skipped:
        parts.append(
            f"Poslední běh vynechal {skipped} nových inzerátů kvůli rozpočtu "
            "- dataset tedy zdaleka není kompletní a doplní se dalšími běhy."
        )
    parts.append(
        "Dokud pokrytí neroste k celému trhu, je nulový výsledek slabý důkaz: "
        "znamená spíš 'ještě jsme tam nedošli' než 'nic tam není'."
    )
    return "\n".join("  " + p for p in parts)


def street_names(rows: list[dict]) -> Counter:
    """Street names in the dataset, counted.

    From `ulice`, which is the parsed street. This used to take the first
    comma-separated component of the raw address and trim a house number off
    it, which was the same work common/address.py already does properly -
    and which stopped working the moment the raw string left the record.
    """
    names: Counter = Counter()
    for row in rows:
        name = (row.get("ulice") or "").strip()
        if name:
            names[name] += 1
    return names


def nearest_street_names(street: str, rows: list[dict], limit: int = 8) -> list[str]:
    """Street names close to the query, by edit distance.

    This exists because of a real miss: a search for "Nurniho" returned
    nothing while the dataset held a listing on **Nurmiho** - one letter
    apart, in the exact district the user cares about. The previous version
    compared a four-character prefix, so a typo in the middle of the word
    slipped straight past it. Fuzzy matching over the actual street names is
    what makes an empty result useful rather than just discouraging.
    """
    needle = fold(street).strip()
    if not needle:
        return []
    names = street_names(rows)
    folded = {fold(name): name for name in names}

    ranked = difflib.get_close_matches(needle, list(folded), n=limit, cutoff=0.6)
    # Substring hits are obviously relevant even when edit distance is not
    # close (e.g. a query for one word of a two-word street name).
    for folded_name, original in folded.items():
        if needle in folded_name and folded_name not in ranked:
            ranked.append(folded_name)
    return [f"{folded[f]} ({names[folded[f]]}x)" for f in ranked[:limit]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("street", help="street name, e.g. Nurniho")
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--include-removed", action="store_true", help="include listings already marked removed")
    parser.add_argument("--property-type", choices=["byt", "dum"], default=None)
    parser.add_argument("--transaction-type", choices=["prodej", "pronajem"], default=None)
    args = parser.parse_args(argv)

    result = filter_street(
        args.street,
        Path(args.data_dir),
        include_removed=args.include_removed,
        property_type=args.property_type,
        transaction_type=args.transaction_type,
    )
    print(result["summary"])
    print(f"[filter_street] zapsáno do {result['out_dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
