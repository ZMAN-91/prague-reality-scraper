from common.geo import (
    PRAGUE_BBOX,
    haversine_distance_m,
    is_in_target_area,
    is_priority_zone,
)


def test_prague_center_is_in_target_area():
    assert is_in_target_area(50.0755, 14.4378) is True


def test_far_outside_prague_is_excluded():
    # Brno city center - clearly outside any reasonable Prague+okoli box.
    assert is_in_target_area(49.1951, 16.6068) is False


def test_missing_gps_is_never_in_target_area():
    assert is_in_target_area(None, None) is False
    assert is_in_target_area(50.0, None) is False


def test_bbox_edges_are_inclusive():
    assert is_in_target_area(PRAGUE_BBOX["lat_min"], PRAGUE_BBOX["lon_min"]) is True
    assert is_in_target_area(PRAGUE_BBOX["lat_max"], PRAGUE_BBOX["lon_max"]) is True


def test_sporilov_center_is_priority_zone():
    """The resolved centre from docs/geo/reference_points.json, not the
    remembered one - which was 1.9 km out and left Roztylske namesti flagged
    as ordinary."""
    assert is_priority_zone(50.04440, 14.47890) is True


def test_roztylske_namesti_is_priority_zone():
    """Regression guard for exactly the failure the old constant caused."""
    assert is_priority_zone(50.04588, 14.47715) is True


def test_hostivar_center_is_priority_zone():
    assert is_priority_zone(50.04890, 14.52440) is True


def test_nurmiho_is_priority_zone():
    """The street the project was asked to watch, by the coordinates of a
    listing actually collected there."""
    assert is_priority_zone(50.04525, 14.52430) is True


def test_prague_castle_is_not_priority_zone():
    # Well outside both priority-zone radii.
    assert is_priority_zone(50.0909, 14.4008) is False


def test_missing_gps_is_never_priority_zone():
    assert is_priority_zone(None, None) is False


def test_haversine_known_distance():
    # Prague <-> Brno, roughly 185 km great-circle.
    d_m = haversine_distance_m(50.0755, 14.4378, 49.1951, 16.6068)
    assert 180_000 < d_m < 190_000


def test_haversine_zero_for_identical_points():
    assert haversine_distance_m(50.0, 14.0, 50.0, 14.0) == 0
