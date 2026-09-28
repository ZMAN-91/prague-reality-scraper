"""The inventory tool: read-only, never writes into this public repository,
and every section runs on a small but real-shaped dataset."""

import gzip
import json
from pathlib import Path

import pytest

from common import storage
from common.schema import LISTING_FIELDS
from tools import inventory


def listing(n, source="sreality", status="active", **extra):
    row = {f: "" for f in LISTING_FIELDS}
    row.update(internal_id=f"{source}:{n}", source=source, source_id=str(n), status=status,
               property_type="byt", transaction_type="prodej", disposition="2+kk",
               area_m2="55", lat="50.03", lon="14.45", mestska_cast="Chodov",
               first_seen_at="2026-09-20T10:00:00+00:00", last_seen_at="2026-09-27",
               url=f"https://example/{source}/{n}")
    row.update(extra)
    return row


@pytest.fixture
def dataset(tmp_path):
    data, logs = tmp_path / "data", tmp_path / "logs"
    rows = [listing(i) for i in range(8)] + [
        listing(100, source="idnes", cluster_id="c1"),
        listing(101, cluster_id="c1"),
        listing(200, status="removed", last_seen_at="2026-09-22"),
        listing(300, area_m2="3"),            # not a flat anyone lives in
        listing(301, lat="48.1", lon="17.1"),  # Bratislava
    ]
    storage.write_listings({r["internal_id"]: r for r in rows}, data / "listings.csv")
    (data / "observations").mkdir()
    (data / "observations" / "2026-09.csv").write_text(
        "internal_id,observed_at,price,price_per_m2,status\n"
        "sreality:200,2026-09-20T10:00:00+00:00,5000000,,active\n"
        "sreality:200,2026-09-23T10:00:00+00:00,,,removed\n"
        "sreality:200,2026-09-24T10:00:00+00:00,5000000,,active\n", encoding="utf-8")
    (data / "state").mkdir()
    (data / "state" / "last_observation.json").write_text(json.dumps(
        {r["internal_id"]: {"price": 5_000_000 + i, "status": "active"} for i, r in enumerate(rows)}),
        encoding="utf-8")
    raw = data / "raw" / "sreality" / "2026-09-27"
    raw.mkdir(parents=True)
    with gzip.open(raw / "index-01.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"pages": [{"response": {"results": [
            {"hash_id": 1, "locality": {"gps_lat": 50.0}, "labels": ["Balkon"]}]}}]}, f)
    logs.mkdir()
    (logs / "2026-09-27.jsonl").write_text(json.dumps({
        "started_at": "2026-09-27T10:00:00+00:00", "finished_at": "2026-09-27T10:40:00+00:00",
        "scope": "area", "ok": True,
        "sources": {"sreality": {"fetched": 10, "new": 1, "errors": [], "interruptions": []}}}) + "\n",
        encoding="utf-8")
    return data, logs


def test_every_section_runs_and_reads_what_it_should(dataset, tmp_path):
    data, logs = dataset
    out = tmp_path / "out"
    assert inventory.main(["--data-dir", str(data), "--logs-dir", str(logs), "--out", str(out)]) == 0
    facts = json.loads((out / "inventory.json").read_text(encoding="utf-8"))

    assert facts["listings"]["total"] == 13
    assert facts["listings"]["implausible"]["area_outside_12_800_m2"] == 1
    assert facts["listings"]["implausible"]["coordinates_outside_prague_box"] == 1
    assert facts["listings"]["clusters"]["cross_portal"] == 1
    assert facts["lifecycle"]["per_source"]["sreality"]["returned_after_removal"] == 1
    assert facts["runs"]["duration_minutes"]["sweep/area"]["p50"] == 40
    assert facts["raw"]["sreality/index"]["fields"]["labels"] == 100.0
    assert facts["growth"]["rows_now"] == 13
    assert facts["growth"]["load_all_listings_now"]["seconds"] is not None
    assert (out / "inventory.md").exists()


def test_it_changes_nothing_in_the_dataset(dataset, tmp_path):
    data, logs = dataset
    before = {p: p.read_bytes() for p in data.rglob("*") if p.is_file()}
    inventory.main(["--data-dir", str(data), "--logs-dir", str(logs), "--out", str(tmp_path / "o")])
    after = {p: p.read_bytes() for p in data.rglob("*") if p.is_file()}
    assert before == after


def test_it_refuses_to_write_into_the_public_repository(dataset, capsys):
    data, logs = dataset
    inside = Path(inventory.__file__).resolve().parent.parent / "inventura-out"
    assert inventory.main(["--data-dir", str(data), "--logs-dir", str(logs), "--out", str(inside)]) == 2
    assert not inside.exists()
