"""Unit tests for #103: GET /locations/map-points' cluster payload gained a
preview `members` sample (capped at 7, newest first) for *every* cluster
(not just small all-stills leaves), plus `date_from`/`date_to` and `place`.

Members are built via `Database.get_map_points` (db/database.py), which does
the SQL-side trim/order/date-range/place aggregation directly — no Python
trimming anymore."""

from datetime import datetime

import pandas as pd
import pytest

from scanner.scanner import ScanResult

LAT, LON = 35.0, 139.0


def _track_file(db, root_dir, name: str, media_type: str = 'photo',
                recorded_at: str | None = None, location_id: int | None = None,
                lat: float = LAT, lon: float = LON):
    """Track one file at a fixed GPS position with optional recorded_at /
    location, mirroring how tests/test_photo_gps.py builds map-point rows."""
    path = root_dir / name
    path.write_bytes(b'not a real file')
    md5_hash = f'hash-{name}'

    db.insert_scan_results([ScanResult(
        md5_hash=md5_hash, file_name=path.name, file_extension=path.suffix,
        media_type=media_type, directory=str(path.parent), last_indexed_at=datetime.now(),
    )])
    db.insert_file_details(pd.DataFrame([{
        'md5_hash': md5_hash, 'latitude': lat, 'longitude': lon,
        'recorded_at': recorded_at, 'location_id': location_id,
    }]))
    return md5_hash


def _get_single_point(db):
    points = db.get_map_points(west=-180, south=-90, east=180, north=90, zoom=1)
    assert len(points) == 1
    return points[0]


def test_cluster_of_ten_returns_count_ten_and_seven_members_newest_first(db, root_dir):
    hashes_by_date = {}
    for i in range(10):
        recorded_at = f'2024:01:{i + 1:02d} 10:00:00'
        h = _track_file(db, root_dir, f'photo_{i}.jpg', recorded_at=recorded_at)
        hashes_by_date[h] = recorded_at

    point = _get_single_point(db)
    assert point['count'] == 10
    assert point['members'] is not None
    assert len(point['members']) == 7

    # Newest first: day 10 down to day 4.
    expected_order = [f'hash-photo_{i}.jpg' for i in range(9, 2, -1)]
    assert [m['md5_hash'] for m in point['members']] == expected_order


def test_cluster_members_without_recorded_at_sort_last(db, root_dir):
    dated = _track_file(db, root_dir, 'dated.jpg', recorded_at='2024:05:01 09:00:00')
    undated_a = _track_file(db, root_dir, 'undated_a.jpg', recorded_at=None)
    undated_b = _track_file(db, root_dir, 'undated_b.jpg', recorded_at=None)

    point = _get_single_point(db)
    assert point['count'] == 3
    members = point['members']
    assert len(members) == 3
    assert members[0]['md5_hash'] == dated
    # Tiebreak among NULL recorded_at members is md5_hash ascending.
    assert [m['md5_hash'] for m in members[1:]] == sorted([undated_a, undated_b])


def test_single_file_cluster_has_one_member_and_date_from_equals_date_to(db, root_dir):
    recorded_at = '2024:03:15 12:30:00'
    h = _track_file(db, root_dir, 'solo.jpg', recorded_at=recorded_at)

    point = _get_single_point(db)
    assert point['count'] == 1
    assert point['members'] is not None
    assert len(point['members']) == 1
    assert point['members'][0]['md5_hash'] == h
    assert point['date_from'] == recorded_at
    assert point['date_to'] == recorded_at


def test_mixed_photo_video_cluster_includes_video_members(db, root_dir):
    _track_file(db, root_dir, 'a.jpg', media_type='photo', recorded_at='2024:01:01 00:00:00')
    video_hash = _track_file(db, root_dir, 'b.mov', media_type='video', recorded_at='2024:02:01 00:00:00')

    point = _get_single_point(db)
    assert point['count'] == 2
    assert point['video_count'] == 1
    assert point['photo_count'] == 1
    media_types = {m['md5_hash']: m['media_type'] for m in point['members']}
    assert media_types[video_hash] == 'video'


def test_date_from_and_date_to_are_min_and_max_of_members(db, root_dir):
    _track_file(db, root_dir, 'mid.jpg', recorded_at='2024:06:15 00:00:00')
    _track_file(db, root_dir, 'early.jpg', recorded_at='2024:01:01 00:00:00')
    _track_file(db, root_dir, 'late.jpg', recorded_at='2024:12:31 23:59:59')

    point = _get_single_point(db)
    assert point['date_from'] == '2024:01:01 00:00:00'
    assert point['date_to'] == '2024:12:31 23:59:59'


def test_date_from_and_date_to_are_none_when_no_member_has_a_date(db, root_dir):
    _track_file(db, root_dir, 'a.jpg', recorded_at=None)
    _track_file(db, root_dir, 'b.jpg', recorded_at=None)

    point = _get_single_point(db)
    assert point['date_from'] is None
    assert point['date_to'] is None


def test_place_is_most_common_city_among_members(db, root_dir):
    tokyo = db.create_location(name=None, city='Tokyo', region=None, country='Japan',
                                latitude=LAT, longitude=LON)
    osaka = db.create_location(name=None, city='Osaka', region=None, country='Japan',
                                latitude=LAT, longitude=LON)

    _track_file(db, root_dir, 'a.jpg', location_id=tokyo)
    _track_file(db, root_dir, 'b.jpg', location_id=tokyo)
    _track_file(db, root_dir, 'c.jpg', location_id=osaka)

    point = _get_single_point(db)
    assert point['place'] == 'Tokyo'


def test_place_falls_back_to_country_when_no_member_has_a_city(db, root_dir):
    location = db.create_location(name=None, city=None, region=None, country='Japan',
                                   latitude=LAT, longitude=LON)
    _track_file(db, root_dir, 'a.jpg', location_id=location)
    _track_file(db, root_dir, 'b.jpg', location_id=location)

    point = _get_single_point(db)
    assert point['place'] == 'Japan'


def test_place_is_none_without_any_location(db, root_dir):
    _track_file(db, root_dir, 'a.jpg')
    _track_file(db, root_dir, 'b.jpg')

    point = _get_single_point(db)
    assert point['place'] is None
