"""Put back the prices a missed sweep erased.

    python -m tools.repair_prices --data-dir store/data [--apply]

Until 2026-09-20, a sweep that did not see a listing wrote price=None into
data/state/last_observation.json for it. The state is what dedup matches on,
so an erased price dissolved the listing's cluster, split one episode into
two, and pushed the supply figure up for every day that episode covered -
including days long closed. run.py no longer does that.

What it leaves behind is the erasures that already happened. A listing seen
again gets its price back by itself; one that is missing, or that was
confirmed removed while it had no price, never does, and its share of the
inflation stays in the series for good.

The observation log is append-only and holds every price ever seen, so it can
say what each of them last cost. This only fills in a missing price and never
overwrites one - a price in the state is a real sighting, and the log cannot
improve on it.

Prints what it would do; --apply writes.
"""

from __future__ import annotations

import argparse
import csv
import glob
import sys
from pathlib import Path

from common import storage


def last_known_prices(observations_dir: Path) -> dict[str, int]:
    """The newest non-empty price each listing was ever observed at."""
    newest: dict[str, tuple[str, int]] = {}
    for path in sorted(glob.glob(str(observations_dir / "*.csv"))):
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                price = (row.get("price") or "").strip()
                if not price:
                    continue
                try:
                    value = int(float(price))
                except ValueError:
                    continue
                seen_at = row.get("observed_at") or ""
                internal_id = row.get("internal_id") or ""
                if not internal_id:
                    continue
                current = newest.get(internal_id)
                if current is None or seen_at >= current[0]:
                    newest[internal_id] = (seen_at, value)
    return {k: v[1] for k, v in newest.items()}


def repair(state: dict[str, dict], prices: dict[str, int]) -> tuple[dict[str, dict], int, int]:
    """Fill blank prices from the log. Returns (state, filled, still blank)."""
    filled = blank = 0
    out = {}
    for internal_id, entry in state.items():
        entry = dict(entry)
        if entry.get("price") is None:
            known = prices.get(internal_id)
            if known is not None:
                entry["price"] = known
                filled += 1
            else:
                # Never observed with a price at all - "cena v RK", or a
                # parse that has never once succeeded. Nothing to put back.
                blank += 1
        out[internal_id] = entry
    return out, filled, blank


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    state_path = data_dir / "state" / "last_observation.json"
    state = storage.read_last_observation_state(state_path)
    if not state:
        print(f"No state at {state_path}, or it could not be read.", file=sys.stderr)
        return 1
    prices = last_known_prices(data_dir / "observations")

    repaired, filled, blank = repair(state, prices)
    print(f"{len(state)} listings in the state, {filled} prices put back, "
          f"{blank} never had one.")
    if not args.apply:
        print("Nothing written; pass --apply.")
        return 0
    # The project's own writer, so the file comes back in the shape every
    # run leaves it in - a repair that rewrites all ten thousand lines is a
    # diff nobody can read.
    storage.write_last_observation_state(repaired, state_path)
    print(f"Written to {state_path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
