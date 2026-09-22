"""The analysis view, tested against the questions it exists to answer.

Each test is one of the traps from tools/episodes.py, written as the wrong
answer someone would get without it.
"""

import csv
import io

import pytest

from common.schema import LISTING_FIELDS
from tools import episodes


def listing(internal_id, first, last, status="removed", source="sreality",
            cluster="", relisted="", area=50.0, ptype="byt"):
    return {
        "internal_id": internal_id, "source": source, "source_id": internal_id,
        "url": f"https://x/{internal_id}", "property_type": ptype,
        "transaction_type": "prodej", "disposition": "2+kk", "area_m2": area,
        "address": "Roztylske namesti", "priority_zone": "True",
        "first_seen_at": f"{first}T10:00:00+00:00", "last_seen_at": last,
        "status": status, "cluster_id": cluster, "dedup_confidence": "high",
        "relisted_from": relisted, "floor": "", "lat": "", "lon": "",
        "description": "",
    }


def obs(internal_id, *pairs):
    return [{"internal_id": internal_id, "observed_at": f"{when}T10:00:00+00:00",
             "price": price, "price_per_m2": "", "status": "active"}
            for when, price in pairs]


def one(rows):
    assert len(rows) == 1, f"expected a single episode, got {len(rows)}"
    return rows[0]


# --- trap 1: duplicates are grouped, not removed ----------------------------


def test_the_same_flat_on_three_portals_is_one_episode():
    """Counting rows counts it three times. On the live data that moved the
    average price of a one-week disappearance by 6.3%."""
    listings = {
        "a": listing("a", "2026-01-01", "2026-01-05", cluster="c1"),
        "b": listing("b", "2026-01-01", "2026-01-05", cluster="c1", source="idnes"),
        "c": listing("c", "2026-01-02", "2026-01-05", cluster="c1", source="bezrealitky"),
    }
    observations = {"a": obs("a", ("2026-01-01", "7000000")),
                    "b": obs("b", ("2026-01-01", "7000000")),
                    "c": obs("c", ("2026-01-02", "7000000"))}
    row = one(episodes.build(listings, observations))
    assert row["listing_count"] == 3
    assert row["sources"] == "bezrealitky|idnes|sreality"
    assert row["last_price"] == 7000000


def test_two_different_flats_stay_two_properties():
    listings = {"a": listing("a", "2026-01-01", "2026-01-05"),
                "b": listing("b", "2026-01-01", "2026-01-05")}
    assert len(episodes.build(listings, {})) == 2


# --- trap 2: re-listing is the long-listing question ------------------------


def test_a_relisted_flat_is_one_episode_not_three():
    """The trap that specifically destroys "listed three months or more":
    a seller who relists monthly looks like three short listings, so the
    properties the question is about are exactly the ones it excludes."""
    listings = {
        "a": listing("a", "2026-01-01", "2026-01-31"),
        "b": listing("b", "2026-02-03", "2026-02-28", relisted="a"),
        "c": listing("c", "2026-03-02", "2026-03-31", relisted="b"),
    }
    observations = {"a": obs("a", ("2026-01-01", "8000000")),
                    "b": obs("b", ("2026-02-03", "7700000")),
                    "c": obs("c", ("2026-03-02", "7400000"))}
    row = one(episodes.build(listings, observations))

    assert row["days_on_market"] >= 89, "the three months were split into three"
    assert row["first_price"] == 8000000 and row["last_price"] == 7400000
    assert row["discount_czk"] == 600000
    assert row["discount_pct"] == 7.5


def test_a_chain_through_a_duplicate_still_joins_up():
    """A relisted as B, B duplicated as C: all one property."""
    listings = {
        "a": listing("a", "2026-01-01", "2026-01-20"),
        "b": listing("b", "2026-01-25", "2026-02-20", relisted="a", cluster="c9"),
        "c": listing("c", "2026-01-26", "2026-02-20", cluster="c9", source="idnes"),
    }
    assert len(episodes.build(listings, {})) == 1


def test_a_long_gap_is_a_new_attempt_to_sell():
    """Relisted after half a year is not one nine-month listing."""
    listings = {"a": listing("a", "2026-01-01", "2026-01-20"),
                "b": listing("b", "2026-08-01", "2026-08-20", relisted="a")}
    rows = episodes.build(listings, {})
    assert len(rows) == 2
    assert [r["episode"] for r in rows] == [1, 2]
    assert rows[1]["gap_before_days"] > 100, "the gap must be reported, not hidden"


def test_the_gap_threshold_can_be_overridden():
    listings = {"a": listing("a", "2026-01-01", "2026-01-20"),
                "b": listing("b", "2026-02-20", "2026-03-01", relisted="a")}
    assert len(episodes.build(listings, {}, gap_days=14)) == 2
    assert len(episodes.build(listings, {}, gap_days=60)) == 1


# --- trap 3: measure to last_seen, not to the removal -----------------------


def test_time_on_market_is_measured_to_the_last_sighting():
    """`removed` arrives three runs later, and for iDNES a sweep later, so
    measuring to it would add source-dependent lag to every duration."""
    listings = {"a": listing("a", "2026-01-01", "2026-01-08")}
    assert one(episodes.build(listings, {}))["days_on_market"] == 7


def test_a_listing_still_on_the_market_says_so():
    listings = {"a": listing("a", "2026-01-01", "2026-01-08", status="active")}
    assert one(episodes.build(listings, {}))["outcome"] == "active"


# --- trap 4: a removal writes an empty price --------------------------------


def test_the_last_price_is_the_last_real_one():
    listings = {"a": listing("a", "2026-01-01", "2026-01-10")}
    observations = {"a": obs("a", ("2026-01-01", "7000000"),
                             ("2026-01-05", "6800000")) +
                    [{"internal_id": "a", "observed_at": "2026-01-11T10:00:00+00:00",
                      "price": "", "price_per_m2": "", "status": "removed"}]}
    row = one(episodes.build(listings, observations))
    assert row["last_price"] == 6800000, "the empty removal price was taken as the last"
    assert row["min_price"] == 6800000 and row["max_price"] == 7000000


def test_a_property_never_seen_with_a_price_is_still_a_row():
    """Missing a price is not a reason to vanish from the history - it is a
    reason for the price columns to be empty."""
    row = one(episodes.build({"a": listing("a", "2026-01-01", "2026-01-10")}, {}))
    assert row["first_price"] == "" and row["discount_pct"] == ""
    assert row["days_on_market"] == 9


# --- the counting of price changes ------------------------------------------


def test_two_portals_disagreeing_is_not_a_price_change():
    """Interleaved observations from two portals quoting slightly different
    figures would otherwise read as the price oscillating every hour."""
    listings = {"a": listing("a", "2026-01-01", "2026-01-10", cluster="c1"),
                "b": listing("b", "2026-01-01", "2026-01-10", cluster="c1",
                             source="idnes")}
    observations = {"a": obs("a", ("2026-01-01", "7000000")),
                    "b": obs("b", ("2026-01-01", "7100000"))}
    assert one(episodes.build(listings, observations))["price_changes"] == 0


def test_a_real_price_change_is_counted_once():
    listings = {"a": listing("a", "2026-01-01", "2026-01-10")}
    observations = {"a": obs("a", ("2026-01-01", "7000000"),
                             ("2026-01-05", "6800000"))}
    assert one(episodes.build(listings, observations))["price_changes"] == 1


# --- the two questions that prompted this file ------------------------------


def test_disappeared_within_a_week_is_one_filter():
    listings = {
        "quick": listing("quick", "2026-01-01", "2026-01-04"),
        "slow": listing("slow", "2026-01-01", "2026-04-01"),
    }
    observations = {"quick": obs("quick", ("2026-01-01", "6000000")),
                    "slow": obs("slow", ("2026-01-01", "9000000"))}
    rows = episodes.build(listings, observations)

    quick = [r for r in rows
             if r["outcome"] == "removed" and r["days_on_market"] <= 7]
    assert len(quick) == 1
    assert sum(r["last_price"] for r in quick) / len(quick) == 6000000


def test_listed_three_months_or_more_is_one_filter():
    listings = {
        "a": listing("a", "2026-01-01", "2026-01-31"),
        "b": listing("b", "2026-02-03", "2026-04-30", relisted="a"),
        "short": listing("short", "2026-01-01", "2026-01-10"),
    }
    observations = {"a": obs("a", ("2026-01-01", "8000000")),
                    "b": obs("b", ("2026-04-30", "7000000")),
                    "short": obs("short", ("2026-01-01", "5000000"))}
    rows = episodes.build(listings, observations)

    long_ones = [r for r in rows if r["days_on_market"] >= 90]
    assert len(long_ones) == 1, "the relisted property was split and so excluded"
    assert long_ones[0]["discount_pct"] == 12.5


# --- the file it writes -----------------------------------------------------


def test_the_export_writes_every_declared_column(tmp_path):
    (tmp_path / "observations").mkdir(parents=True)
    writer_fields = LISTING_FIELDS
    with open(tmp_path / "listings.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=writer_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerow(listing("a", "2026-01-01", "2026-01-10"))
    with open(tmp_path / "observations" / "2026-01.csv", "w",
              encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["internal_id", "observed_at",
                                               "price", "price_per_m2", "status"])
        writer.writeheader()
        writer.writerows(obs("a", ("2026-01-01", "7000000")))

    stats = episodes.export(tmp_path)
    assert stats["episodes"] == 1

    with open(stats["path"], encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == episodes.FIELDS
    assert rows[0]["last_price"] == "7000000"


def test_observations_are_read_from_every_month(tmp_path):
    """An episode spanning a year spans twelve files, and the long-listing
    question is about precisely those."""
    (tmp_path / "observations").mkdir(parents=True)
    for month in ("2026-01", "2026-02"):
        with open(tmp_path / "observations" / f"{month}.csv", "w",
                  encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["internal_id", "observed_at",
                                                   "price", "price_per_m2", "status"])
            writer.writeheader()
            writer.writerows(obs("a", (f"{month}-05", "7000000")))
    loaded = episodes.load_observations(tmp_path)
    assert len(loaded["a"]) == 2
    assert loaded["a"][0]["observed_at"] < loaded["a"][1]["observed_at"]


# --- on the market, or gone? ------------------------------------------------


def test_a_temporarily_missing_listing_has_not_left_the_market():
    """Reading "not active" as "gone" counted 51 of 10 990 live listings as
    departures, and would turn every portal hiccup into a wave of departures
    followed by a wave of arrivals when they came back.

    Still true, and it is what `disappearing` is for: not seen, not gone.
    The things that count departures ask for `removed` exactly, so this
    state is a departure to none of them.
    """
    for status in ("missing_1", "missing_2", "missing_3"):
        listings = {"a": listing("a", "2026-01-01", "2026-01-10", status=status)}
        assert one(episodes.build(listings, {}))["outcome"] == "disappearing", \
            f"{status} was not reported as on its way out"
        assert one(episodes.build(listings, {}))["outcome"] != "removed", \
            f"{status} was counted as a departure"


def test_an_episode_on_its_way_out_says_how_long_it_has_been_gone():
    """The whole point of the state: "gone three days" has to be readable
    off the sheet, not derived from two date columns by whoever opens it."""
    from datetime import date
    listings = {"a": listing("a", "2026-01-01", "2026-01-10", status="missing_2")}
    row = one(episodes.build(listings, {}, today=date(2026, 1, 13)))
    assert row["outcome"] == "disappearing"
    assert row["days_missing"] == 3


def test_a_listing_still_being_seen_is_not_missing_for_any_days():
    from datetime import date
    listings = {"a": listing("a", "2026-01-01", "2026-01-10", status="active")}
    row = one(episodes.build(listings, {}, today=date(2026, 1, 13)))
    assert row["outcome"] == "active"
    assert row["days_missing"] == 0


def test_days_on_market_does_not_include_the_waiting_period():
    """A departure is confirmed REMOVAL_AFTER_DAYS after the last sighting.
    If that tail were added, every departure would carry this project's
    patience in its time-on-market figure rather than the market's."""
    from datetime import date
    listings = {"a": listing("a", "2026-01-01", "2026-01-10", status="removed")}
    row = one(episodes.build(listings, {}, today=date(2026, 1, 20)))
    assert row["days_on_market"] == 9, "the probation window leaked in"
    assert row["days_missing"] == 10


def test_only_a_confirmed_removal_is_a_departure():
    listings = {"a": listing("a", "2026-01-01", "2026-01-10", status="removed")}
    assert one(episodes.build(listings, {}))["outcome"] == "removed"


def test_one_advert_still_up_keeps_the_property_on_the_market():
    listings = {
        "a": listing("a", "2026-01-01", "2026-01-10", status="removed", cluster="c1"),
        "b": listing("b", "2026-01-01", "2026-01-10", status="active",
                     cluster="c1", source="idnes"),
    }
    observations = {"a": obs("a", ("2026-01-01", "7000000")),
                    "b": obs("b", ("2026-01-01", "7000000"))}
    assert one(episodes.build(listings, observations))["outcome"] == "active"


# --- when things happened, not just how often -------------------------------


def test_the_day_of_each_price_cut_is_recorded():
    """A total count cannot answer "how many discounted in the last 30 days",
    which is the question actually worth asking."""
    listings = {"a": listing("a", "2026-01-01", "2026-03-01", status="active")}
    observations = {"a": obs("a", ("2026-01-01", "8000000"),
                             ("2026-01-20", "7600000"),
                             ("2026-02-15", "7200000"))}
    row = one(episodes.build(listings, observations))
    assert row["price_changes"] == 2
    assert row["cut_days"] == "2026-01-20|2026-02-15"


def test_the_day_of_each_edit_is_recorded():
    listings = {"a": listing("a", "2026-01-01", "2026-03-01", status="active")}
    changes = {"a": [{"changed_at": "2026-02-10T10:00:00+00:00", "field": "description"},
                     {"changed_at": "2026-01-05T10:00:00+00:00", "field": "disposition"}]}
    row = one(episodes.build(listings, {}, changes=changes))
    assert row["attribute_changes"] == 2
    assert row["edit_days"] == "2026-01-05|2026-02-10", "sorted, oldest first"


def test_a_property_that_never_moved_has_empty_day_lists():
    listings = {"a": listing("a", "2026-01-01", "2026-01-10", status="active")}
    row = one(episodes.build(listings, {}))
    assert row["cut_days"] == "" and row["edit_days"] == ""
