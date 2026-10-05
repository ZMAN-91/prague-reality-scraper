"""The one-off clean-up of adverts the dataset no longer takes."""

import gzip
import json

from common import progress as progress_state
from common import storage
from common.schema import LISTING_FIELDS
from tools import exclude_bezrealitky


def row(source, source_id, status="active"):
    r = {f: "" for f in LISTING_FIELDS}
    r.update(internal_id=f"{source}:{source_id}", source=source, source_id=source_id,
             status=status, last_seen_at="2026-09-20", url=f"https://x/{source_id}")
    return r


def test_it_removes_every_excluded_row_whatever_its_status(tmp_path):
    """Removed adverts are never read again, so only the raw archive can
    say they were FLATIO lets or outside Prague."""
    data = tmp_path / "data"
    rows = [row("bezrealitky", "1"), row("bezrealitky", "2", "removed"),
            row("bezrealitky", "3"), row("sreality", "1")]
    storage.write_listings({r["internal_id"]: r for r in rows}, data / "listings.csv")
    raw = data / "raw" / "bezrealitky" / "2026-09-20"
    raw.mkdir(parents=True)
    with gzip.open(raw / "detail-01.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"pages": [{"response": {"id": 1, "type": "FLATIO"}},
                             {"response": {"id": 2, "type": "UNDEFINED", "isPrague": False}},
                             {"response": {"id": 3, "type": "UNDEFINED", "isPrague": True}},
                             {"response": {"id": 1, "type": "FLATIO"}}]}, f)

    assert exclude_bezrealitky.main(["--data-dir", str(data)]) == 0
    assert len(storage.read_listings(data / "listings.csv")) == 4, "a dry run writes nothing"

    exclude_bezrealitky.main(["--data-dir", str(data), "--apply"])
    left = storage.read_listings(data / "listings.csv")
    assert set(left) == {"bezrealitky:3", "sreality:1"}
    memory = progress_state.read(data)["bezrealitky"]["vyrazene"]
    assert memory == {"1": "flatio", "2": "mimo-prahu"}
