"""A sweep that did not see a listing must not erase what it last cost.

Found by watching a closed day's supply move on its own: 2026-09-19 read
4615, then 4641, then 4618 across three consecutive runs, with nothing having
happened in the market. The cause was one run marking 228 listings absent and
writing price=None into the observation state. Dedup matches on price, so a
listing without one stopped matching, its cluster dissolved, its two rows
became two episodes, and supply went up by one for every day the episode
covered. Sale clusters went 693 -> 671 and back.

The observation row for an absence still carries no price, which is the
honest reading. The remembered price is a different question.
"""

from common.schema import NormalizedListing
from run import merge_source

SCOPES = {("byt", "prodej")}


def listing(price=6_940_000, source_id="1"):
    return NormalizedListing(
        source="sreality", source_id=source_id,
        url=f"https://www.sreality.cz/detail/{source_id}",
        property_type="byt", transaction_type="prodej",
        disposition="2+kk", area_m2=55.0, price=price,
        lat=50.0400, lon=14.4800, address="Roztylske namesti, Praha",
        description="Hezky byt",
    )


def sweep(normalized, listings, last_obs, when):
    return merge_source("sreality", normalized, [], listings, last_obs, when,
                        [], SCOPES)


def seen_once():
    listings, last_obs = {}, {}
    sweep([listing()], listings, last_obs, "2026-09-19T10:00:00+00:00")
    return listings, last_obs


def only_key(last_obs):
    assert len(last_obs) == 1, last_obs
    return next(iter(last_obs.values()))


def test_a_listing_missed_by_one_sweep_keeps_its_price():
    listings, last_obs = seen_once()
    assert only_key(last_obs)["price"] == 6_940_000

    sweep([], listings, last_obs, "2026-09-20T06:16:00+00:00")
    state = only_key(last_obs)
    assert state["status"].startswith("missing"), state
    assert state["price"] == 6_940_000, "the absence erased the last price"


def test_the_absence_is_still_recorded_without_a_price():
    """We did not see it, so we have no price for it today - the row says so
    even though the state remembers."""
    listings, last_obs = seen_once()
    _, rows, _ = sweep([], listings, last_obs, "2026-09-20T06:16:00+00:00")
    absence = [r for r in rows if r["status"].startswith("missing")]
    assert len(absence) == 1, rows
    assert absence[0]["price"] == ""


def test_a_listing_absent_for_hours_does_not_log_a_row_an_hour():
    """The status only moves once a day, and the price no longer flaps
    between a number and nothing - so an unchanged absence writes nothing."""
    listings, last_obs = seen_once()
    sweep([], listings, last_obs, "2026-09-20T06:16:00+00:00")
    _, rows, _ = sweep([], listings, last_obs, "2026-09-20T07:16:00+00:00")
    assert rows == [], rows


def test_the_same_price_coming_back_is_not_a_price_change():
    """Before the fix the state held None while the listing was away, so its
    return compared None against the price and read as news."""
    listings, last_obs = seen_once()
    sweep([], listings, last_obs, "2026-09-20T06:16:00+00:00")
    _, rows, _ = sweep([listing()], listings, last_obs, "2026-09-20T07:16:00+00:00")
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "active"
    assert only_key(last_obs)["price"] == 6_940_000


def test_a_real_price_cut_while_it_was_away_is_still_seen():
    """Keeping the old price must not swallow the new one."""
    listings, last_obs = seen_once()
    sweep([], listings, last_obs, "2026-09-20T06:16:00+00:00")
    _, rows, _ = sweep([listing(price=6_500_000)], listings, last_obs,
                       "2026-09-20T07:16:00+00:00")
    assert [r["price"] for r in rows] == [6_500_000], rows
    assert only_key(last_obs)["price"] == 6_500_000
