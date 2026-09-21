import gzip
import json
from datetime import datetime, timezone

from common import storage


def test_raw_archive_roundtrip(tmp_path):
    fetched_at = datetime(2026, 3, 15, 9, tzinfo=timezone.utc)
    out_path = storage.write_raw_archive("sreality", fetched_at, {"pages": [1, 2, 3]}, raw_dir=tmp_path)

    assert out_path == tmp_path / "sreality" / "2026-03-15" / "index-09.json.gz"
    with gzip.open(out_path, "rt", encoding="utf-8") as f:
        assert json.load(f) == {"pages": [1, 2, 3]}


def test_raw_archive_overwrites_same_hour_on_rerun(tmp_path):
    fetched_at = datetime(2026, 3, 15, 9, tzinfo=timezone.utc)
    storage.write_raw_archive("sreality", fetched_at, {"pages": [1]}, raw_dir=tmp_path)
    out_path = storage.write_raw_archive("sreality", fetched_at, {"pages": [2]}, raw_dir=tmp_path)
    with gzip.open(out_path, "rt", encoding="utf-8") as f:
        assert json.load(f) == {"pages": [2]}


def test_raw_archive_index_is_throttled_to_once_per_day(tmp_path):
    """The index dump is the entire active market; writing it hourly into a
    git repo is what would make this project outgrow GitHub within months."""
    first = storage.write_raw_archive(
        "sreality", datetime(2026, 3, 15, 9, tzinfo=timezone.utc),
        {"pages": [1]}, raw_dir=tmp_path, kind="index", once_per_day=True,
    )
    second = storage.write_raw_archive(
        "sreality", datetime(2026, 3, 15, 10, tzinfo=timezone.utc),
        {"pages": [2]}, raw_dir=tmp_path, kind="index", once_per_day=True,
    )
    assert first is not None
    assert second is None  # skipped, same day
    assert len(list((tmp_path / "sreality" / "2026-03-15").glob("index-*.json.gz"))) == 1

    # A new day writes again.
    next_day = storage.write_raw_archive(
        "sreality", datetime(2026, 3, 16, 0, tzinfo=timezone.utc),
        {"pages": [3]}, raw_dir=tmp_path, kind="index", once_per_day=True,
    )
    assert next_day is not None


def test_raw_archive_detail_is_never_throttled(tmp_path):
    """Detail payloads are fetched once per listing ever and are
    irreplaceable, so every one of them is archived."""
    day = datetime(2026, 3, 15, 9, tzinfo=timezone.utc)
    storage.write_raw_archive("sreality", day, {"pages": [1]}, raw_dir=tmp_path, kind="index", once_per_day=True)
    a = storage.write_raw_archive("sreality", day, {"pages": [1]}, raw_dir=tmp_path, kind="detail")
    b = storage.write_raw_archive(
        "sreality", datetime(2026, 3, 15, 11, tzinfo=timezone.utc),
        {"pages": [2]}, raw_dir=tmp_path, kind="detail",
    )
    assert a is not None and b is not None and a != b


def test_listings_roundtrip(tmp_path):
    path = tmp_path / "listings.csv"
    assert storage.read_listings(path) == {}

    row = {
        "internal_id": "abc123",
        "source": "sreality",
        "source_id": "999",
        "url": "https://example.com/999",
        "property_type": "byt",
        "transaction_type": "prodej",
        "disposition": "2+kk",
        "area_m2": "55.0",
        "floor": "3",
        "lat": "50.0755",
        "lon": "14.4378",
        "gps_zdroj": "",
        "address": "Praha 10",
        "ulice": "",
        "cislo_popisne": "",
        "cislo_orientacni": "",
        "cislo_zdroj": "",
        "cislo_vzdalenost_m": "",
        "cislo_kandidatu": "",
        "mestska_cast": "Praha 10",
        "obec": "Praha",
        "priority_zone": "False",
        "description": "hezky byt",
        "first_seen_at": "2026-01-01T00:00:00+00:00",
        "last_seen_at": "2026-01-02T00:00:00+00:00",
        "status": "active",
        "cluster_id": "",
        "dedup_confidence": "",
        "relisted_from": "",
    }
    storage.write_listings({"abc123": row}, path)

    loaded = storage.read_listings(path)
    assert loaded == {"abc123": row}


def test_every_schema_field_survives_a_write_read_roundtrip(tmp_path):
    """Regression guard for a bug this suite originally missed: dedup.py set
    `dedup_confidence` on every clustered row, but the column was absent
    from LISTING_FIELDS and DictWriter(extrasaction="ignore") dropped it
    silently on write. Asserting on the in-memory dict could never catch
    that - only a real round-trip through the CSV can."""
    from common.schema import LISTING_FIELDS

    path = tmp_path / "listings.csv"
    row = {field: f"v-{field}" for field in LISTING_FIELDS}
    row["internal_id"] = "rt1"
    storage.write_listings({"rt1": row}, path)

    loaded = storage.read_listings(path)["rt1"]
    assert set(loaded) == set(LISTING_FIELDS)
    for field in LISTING_FIELDS:
        assert loaded[field] == row[field], f"{field} did not survive the round-trip"


def test_listings_roundtrip_preserves_awkward_text(tmp_path):
    """Czech descriptions routinely contain commas, quotes, semicolons and
    newlines - all of which are CSV landmines."""
    from common.schema import LISTING_FIELDS

    nasty = 'Byt 2+kk, "luxusní"; s výhledem\nna park — 55 m²\r\nblízko metra'
    path = tmp_path / "listings.csv"
    row = {field: "" for field in LISTING_FIELDS}
    row.update({"internal_id": "x1", "description": nasty, "address": "Nurniho 5, Praha 4"})
    storage.write_listings({"x1": row}, path)

    loaded = storage.read_listings(path)["x1"]
    # Line breaks are deliberately collapsed to spaces on write - see
    # schema.clean_text: kept verbatim they turned 2 682 rows into 23 848
    # physical lines and made every diff unreadable. Everything else - the
    # commas, the quotes, the semicolon, the em dash, the superscript - must
    # survive byte for byte, because those are the actual CSV landmines.
    assert loaded["description"] == 'Byt 2+kk, "luxusní"; s výhledem na park — 55 m² blízko metra'
    assert loaded["address"] == "Nurniho 5, Praha 4"


def test_listings_write_is_atomic_no_partial_file_left_behind(tmp_path):
    path = tmp_path / "listings.csv"
    storage.write_listings({}, path)
    assert path.exists()
    # No leftover temp files.
    assert list(tmp_path.glob(".tmp-*")) == []


def test_observations_append_creates_header_once(tmp_path):
    obs_dir = tmp_path / "observations"
    when = datetime(2026, 5, 1, tzinfo=timezone.utc)

    row1 = {"internal_id": "a", "observed_at": "t1", "price": "100", "price_per_m2": "2", "status": "active"}
    row2 = {"internal_id": "b", "observed_at": "t2", "price": "200", "price_per_m2": "4", "status": "active"}

    n1 = storage.append_observations([row1], when, obs_dir)
    n2 = storage.append_observations([row2], when, obs_dir)
    assert n1 == 1 and n2 == 1

    path = storage.observations_path_for(when, obs_dir)
    text = path.read_text(encoding="utf-8")
    assert text.count("internal_id,observed_at") == 1  # header written only once
    assert "a,t1,100,2,active" in text
    assert "b,t2,200,4,active" in text


def test_observations_bucketed_by_month():
    jan = storage.observations_path_for(datetime(2026, 1, 15, tzinfo=timezone.utc))
    feb = storage.observations_path_for(datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert jan.name == "2026-01.csv"
    assert feb.name == "2026-02.csv"


def test_last_observation_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    assert storage.read_last_observation_state(path) == {}
    state = {"abc123": {"price": 100, "status": "active"}}
    storage.write_last_observation_state(state, path)
    assert storage.read_last_observation_state(path) == state


def test_last_observation_state_corrupt_file_recovers_to_empty(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not valid json{{{", encoding="utf-8")
    assert storage.read_last_observation_state(path) == {}


def test_empty_payload_is_never_archived(tmp_path):
    """A failed run used to write a 47-byte {"pages": []} file which, under
    once_per_day, occupied the day's slot and silently stopped the
    *successful* run an hour later from archiving anything at all."""
    day = datetime(2026, 3, 15, 9, tzinfo=timezone.utc)
    assert storage.write_raw_archive(
        "sreality", day, {"pages": []}, raw_dir=tmp_path, kind="index", once_per_day=True
    ) is None
    assert not list(tmp_path.rglob("*.json.gz"))

    # The later, successful run of the same day still gets its slot.
    later = storage.write_raw_archive(
        "sreality", datetime(2026, 3, 15, 10, tzinfo=timezone.utc),
        {"pages": [{"response": {"x": 1}}]}, raw_dir=tmp_path, kind="index", once_per_day=True,
    )
    assert later is not None


def test_image_galleries_are_stripped_from_the_archive(tmp_path):
    """Measured on real payloads, image URL arrays were ~44% of the archived
    bytes - for a project whose brief says photos are not collected."""
    payload = {
        "pages": [
            {
                "response": {
                    "hash_id": 1,
                    "advert_name": "Prodej bytu 2+kk",
                    "advert_images": [{"url": "https://img/1.jpg"}] * 30,
                    "locality": {"gps_lat": 50.0, "advert_images_all": ["x"] * 10},
                }
            }
        ]
    }
    path = storage.write_raw_archive("sreality", datetime(2026, 3, 15, 9, tzinfo=timezone.utc),
                                     payload, raw_dir=tmp_path, kind="detail")
    with gzip.open(path, "rt", encoding="utf-8") as f:
        archived = json.load(f)
    response = archived["pages"][0]["response"]
    assert "advert_images" not in response
    assert "advert_images_all" not in response["locality"]
    # Everything else survives verbatim, including nested values.
    assert response["advert_name"] == "Prodej bytu 2+kk"
    assert response["locality"]["gps_lat"] == 50.0


def test_descriptions_are_written_as_one_line_each(tmp_path):
    """A row per line is what makes a diff of an hourly-rewritten file
    readable: 2 682 listings should be 2 683 lines, not 23 848."""
    from common.schema import LISTING_FIELDS

    path = tmp_path / "listings.csv"
    rows = {}
    for n in range(50):
        row = {field: "" for field in LISTING_FIELDS}
        row.update({
            "internal_id": f"id{n}",
            "description": f"Radek jedna\nradek dva\r\nradek tri {n}",
            "address": "Nurmiho,\nPraha",
        })
        rows[f"id{n}"] = row
    storage.write_listings(rows, path)

    physical_lines = sum(1 for _ in path.open(encoding="utf-8"))
    assert physical_lines == 51, f"expected one header + 50 rows, got {physical_lines}"


def test_a_row_written_by_an_older_version_is_tidied_on_the_next_save(tmp_path):
    """Normalising only at parse time would leave everything already on disk
    ragged forever."""
    from common.schema import LISTING_FIELDS

    path = tmp_path / "listings.csv"
    row = {field: "" for field in LISTING_FIELDS}
    row.update({"internal_id": "old", "description": "stary\nzapis", "address": "Ulice"})
    storage.write_listings({"old": row}, path)
    assert "\n" not in storage.read_listings(path)["old"]["description"]


def test_clean_text_leaves_a_normal_description_alone():
    from common.schema import clean_text

    text = "Prostorny byt 2+kk s balkonem, 55 m2."
    assert clean_text(text) == text
    assert clean_text(None) is None
    assert clean_text("   ") is None


def test_writing_identical_content_does_not_touch_the_file(tmp_path):
    """Every file here is committed, so rewriting an identical file still
    makes a commit with a zero-line diff - a quiet hour looking like a busy
    one."""
    import os

    from common.schema import LISTING_FIELDS

    path = tmp_path / "listings.csv"
    row = {field: "" for field in LISTING_FIELDS}
    row["internal_id"] = "x1"
    storage.write_listings({"x1": row}, path)
    before = os.stat(path).st_mtime_ns

    storage.write_listings({"x1": row}, path)
    assert os.stat(path).st_mtime_ns == before, "an identical write must be skipped"


def test_a_real_change_is_still_written(tmp_path):
    from common.schema import LISTING_FIELDS

    path = tmp_path / "listings.csv"
    row = {field: "" for field in LISTING_FIELDS}
    row["internal_id"] = "x1"
    storage.write_listings({"x1": row}, path)

    row["status"] = "removed"
    storage.write_listings({"x1": row}, path)
    assert "removed" in path.read_text(encoding="utf-8")
