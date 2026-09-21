"""Re-parsing addresses on rows collected before the fields existed."""

import pytest

from tools import backfill_address


def row(address, **extra):
    base = {"internal_id": "a", "address": address, "ulice": "",
            "mestska_cast": "", "obec": ""}
    base.update(extra)
    return base


def test_a_blank_row_is_filled():
    listings = {"a": row("Roháčova, Praha 3 - Žižkov")}
    changed, _ = backfill_address.backfill(listings)
    assert changed == 1
    assert listings["a"]["ulice"] == "Rohacova"
    assert listings["a"]["mestska_cast"] == "Zizkov"


def test_the_raw_address_is_never_touched():
    """It is what the portal said, and the four fields are derived from it -
    rewriting the evidence to match the derivation gets that backwards."""
    listings = {"a": row("Roháčova, Praha 3 - Žižkov")}
    backfill_address.backfill(listings)
    assert listings["a"]["address"] == "Roháčova, Praha 3 - Žižkov"


def test_running_twice_changes_nothing_the_second_time():
    listings = {"a": row("Ke Slatinam, Dolni Mecholupy, Praha")}
    backfill_address.backfill(listings)
    changed, _ = backfill_address.backfill(listings)
    assert changed == 0


def test_a_re_parse_that_disagrees_wins():
    """The point of being re-runnable: when the parser learns a shape, the
    whole history gets the improvement, not just rows collected after it."""
    listings = {"a": row("Hostivar, Praha", ulice="Hostivar", mestska_cast="")}
    changed, _ = backfill_address.backfill(listings)
    assert changed == 1
    assert listings["a"]["ulice"] == ""
    assert listings["a"]["mestska_cast"] == "Hostivar"


def test_coverage_counts_only_fields_that_got_a_value():
    listings = {"a": row("Štichova, Praha"), "b": row("Praha")}
    _, coverage = backfill_address.backfill(listings)
    assert coverage["obec"] == 2
    assert coverage["ulice"] == 1          # only Stichova
    assert coverage["mestska_cast"] == 0
    assert "cislo_popisne" not in coverage, (
        "the address parser has started claiming the house number field "
        "again; see common/address.py")


def test_an_unparseable_address_leaves_empty_fields_not_an_error():
    listings = {"a": row("")}
    changed, coverage = backfill_address.backfill(listings)
    assert changed == 0
    assert coverage["obec"] == 0
