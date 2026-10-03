"""HTTP-level tests for the path-conflicts endpoints added in #25:
GET /tracking/conflicts(/count), POST /tracking/conflicts/resolve(-batch)."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.tracking import TrackingApi
from db.engine import get_engine
from db.models import (
    file_details_table,
    file_keywords_table,
    files_table,
    keywords_table,
    list_items_table,
    lists_table,
    path_conflicts_table,
)


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(TrackingApi)
    return TestClient(app)


def _insert_file_row(directory: str, file_name: str, md5_hash: str, media_type='photo'):
    with get_engine().begin() as conn:
        conn.execute(files_table.insert().values(
            md5_hash=md5_hash, file_name=file_name,
            file_extension=Path(file_name).suffix, media_type=media_type,
            directory=directory,
        ))


def _insert_conflict(md5_hash: str, candidate_path: str, source='rediscover'):
    with get_engine().begin() as conn:
        conn.execute(path_conflicts_table.insert().values(
            md5_hash=md5_hash, candidate_path=candidate_path, source=source,
        ))


# ---------------------------------------------------------------------------
# GET /tracking/conflicts + /conflicts/count
# ---------------------------------------------------------------------------

def test_conflicts_listing_shape(db, root_dir):
    client = _make_client()
    tracked_dir = root_dir / 'tracked'
    tracked_dir.mkdir()
    copy_dir = root_dir / 'copy'
    copy_dir.mkdir()

    tracked_file = tracked_dir / 'f.jpg'
    tracked_file.write_bytes(b'x')
    copy_file = copy_dir / 'f.jpg'
    copy_file.write_bytes(b'x')

    md5 = 'h1'
    _insert_file_row(str(tracked_dir), 'f.jpg', md5)
    _insert_conflict(md5, str(copy_file))

    with get_engine().begin() as conn:
        conn.execute(keywords_table.insert().values(id=1, keyword='sunset'))
        conn.execute(file_keywords_table.insert().values(md5_hash=md5, keyword_id=1))
        conn.execute(lists_table.insert().values(id=1, name='Favorites'))
        conn.execute(list_items_table.insert().values(list_id=1, md5_hash=md5, item_code='ABC123'))

    resp = client.get('/tracking/conflicts')
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    entry = body[0]
    assert entry['md5_hash'] == md5
    assert entry['file_name'] == 'f.jpg'
    assert entry['tracked_path'] == str(tracked_file)
    assert entry['tracked_exists'] is True
    assert entry['keyword_count'] == 1
    assert entry['list_count'] == 1
    assert entry['has_location'] is False
    assert entry['has_preview'] is False
    assert entry['candidates'] == [{
        'path': str(copy_file), 'exists': True, 'source': 'rediscover', 'found_at': entry['candidates'][0]['found_at'],
    }]

    count_resp = client.get('/tracking/conflicts/count')
    assert count_resp.status_code == 200
    assert count_resp.json() == {'count': 1}


def test_conflicts_count_zero_when_none(db, root_dir):
    client = _make_client()
    assert client.get('/tracking/conflicts/count').json() == {'count': 0}
    assert client.get('/tracking/conflicts').json() == []


# ---------------------------------------------------------------------------
# POST /tracking/conflicts/resolve
# ---------------------------------------------------------------------------

def test_resolve_to_candidate_updates_path_and_clears_rows_metadata_intact(db, root_dir):
    client = _make_client()
    tracked_dir = root_dir / 'tracked'
    tracked_dir.mkdir()
    copy_dir = root_dir / 'copy'
    copy_dir.mkdir()
    copy_file = copy_dir / 'f.jpg'
    copy_file.write_bytes(b'x')
    # Note: the old tracked path does NOT exist on disk anymore (simulating
    # the move that created the conflict's sibling copy scenario is moot
    # here — what matters is resolve doesn't care, it trusts the request).

    md5 = 'h1'
    _insert_file_row(str(tracked_dir), 'f.jpg', md5)
    _insert_conflict(md5, str(copy_file))
    with get_engine().begin() as conn:
        conn.execute(keywords_table.insert().values(id=1, keyword='sunset'))
        conn.execute(file_keywords_table.insert().values(md5_hash=md5, keyword_id=1))

    resp = client.post('/tracking/conflicts/resolve', json={
        'md5_hash': md5, 'chosen_path': str(copy_file),
    })
    assert resp.status_code == 204

    with get_engine().connect() as conn:
        row = conn.execute(files_table.select().where(files_table.c.md5_hash == md5)).fetchone()
        assert row.directory == str(copy_dir)
        assert row.file_name == 'f.jpg'

        remaining = conn.execute(
            path_conflicts_table.select().where(path_conflicts_table.c.md5_hash == md5)
        ).fetchall()
        assert remaining == []

        kw = conn.execute(
            file_keywords_table.select().where(file_keywords_table.c.md5_hash == md5)
        ).fetchall()
        assert len(kw) == 1


def test_resolve_keep_tracked_clears_rows_without_path_change(db, root_dir):
    client = _make_client()
    tracked_dir = root_dir / 'tracked'
    tracked_dir.mkdir()
    tracked_file = tracked_dir / 'f.jpg'
    tracked_file.write_bytes(b'x')
    copy_dir = root_dir / 'copy'
    copy_dir.mkdir()
    copy_file = copy_dir / 'f.jpg'
    copy_file.write_bytes(b'x')

    md5 = 'h1'
    _insert_file_row(str(tracked_dir), 'f.jpg', md5)
    _insert_conflict(md5, str(copy_file))

    resp = client.post('/tracking/conflicts/resolve', json={
        'md5_hash': md5, 'chosen_path': str(tracked_file),
    })
    assert resp.status_code == 204

    with get_engine().connect() as conn:
        row = conn.execute(files_table.select().where(files_table.c.md5_hash == md5)).fetchone()
        assert row.directory == str(tracked_dir)
        remaining = conn.execute(
            path_conflicts_table.select().where(path_conflicts_table.c.md5_hash == md5)
        ).fetchall()
        assert remaining == []


def test_resolve_invalid_chosen_path_is_400(db, root_dir):
    client = _make_client()
    tracked_dir = root_dir / 'tracked'
    tracked_dir.mkdir()
    (tracked_dir / 'f.jpg').write_bytes(b'x')
    copy_dir = root_dir / 'copy'
    copy_dir.mkdir()
    copy_file = copy_dir / 'f.jpg'
    copy_file.write_bytes(b'x')

    md5 = 'h1'
    _insert_file_row(str(tracked_dir), 'f.jpg', md5)
    _insert_conflict(md5, str(copy_file))

    resp = client.post('/tracking/conflicts/resolve', json={
        'md5_hash': md5, 'chosen_path': str(root_dir / 'unrelated.jpg'),
    })
    assert resp.status_code == 400


def test_resolve_missing_chosen_file_is_409(db, root_dir):
    client = _make_client()
    tracked_dir = root_dir / 'tracked'
    tracked_dir.mkdir()
    (tracked_dir / 'f.jpg').write_bytes(b'x')
    copy_dir = root_dir / 'copy'
    copy_dir.mkdir()
    copy_file = copy_dir / 'f.jpg'
    # Never written to disk -> candidate doesn't exist.

    md5 = 'h1'
    _insert_file_row(str(tracked_dir), 'f.jpg', md5)
    _insert_conflict(md5, str(copy_file))

    resp = client.post('/tracking/conflicts/resolve', json={
        'md5_hash': md5, 'chosen_path': str(copy_file),
    })
    assert resp.status_code == 409


def test_resolve_no_conflicts_for_hash_is_404(db, root_dir):
    client = _make_client()
    _insert_file_row(str(root_dir), 'f.jpg', 'h1')
    resp = client.post('/tracking/conflicts/resolve', json={
        'md5_hash': 'h1', 'chosen_path': str(root_dir / 'f.jpg'),
    })
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /tracking/conflicts/resolve-batch
# ---------------------------------------------------------------------------

def test_batch_keep_tracked_resolves_and_skips_missing(db, root_dir):
    client = _make_client()

    ok_dir = root_dir / 'ok'
    ok_dir.mkdir()
    (ok_dir / 'f.jpg').write_bytes(b'x')
    _insert_file_row(str(ok_dir), 'f.jpg', 'h_ok')
    _insert_conflict('h_ok', str(root_dir / 'elsewhere.jpg'))

    gone_dir = root_dir / 'gone'
    gone_dir.mkdir()
    # tracked file itself doesn't exist on disk
    _insert_file_row(str(gone_dir), 'f.jpg', 'h_gone')
    _insert_conflict('h_gone', str(root_dir / 'elsewhere2.jpg'))

    resp = client.post('/tracking/conflicts/resolve-batch', json={
        'strategy': 'keep_tracked', 'md5_hashes': ['h_ok', 'h_gone'],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body['resolved'] == 1
    assert len(body['skipped']) == 1
    assert body['skipped'][0]['md5_hash'] == 'h_gone'

    # h_ok resolved and cleared; h_gone's conflict is left open (skipped).
    assert client.get('/tracking/conflicts/count').json() == {'count': 1}


def test_batch_use_candidate_resolves_single_existing_skips_ambiguous_and_none(db, root_dir):
    client = _make_client()

    # h_single: exactly one existing candidate -> resolved.
    single_dir = root_dir / 'single'
    single_dir.mkdir()
    candidate = single_dir / 'copy.jpg'
    candidate.write_bytes(b'x')
    _insert_file_row(str(root_dir), 'tracked_single.jpg', 'h_single')
    _insert_conflict('h_single', str(candidate))

    # h_ambiguous: two existing candidates -> skipped.
    amb_dir = root_dir / 'amb'
    amb_dir.mkdir()
    c1 = amb_dir / 'c1.jpg'
    c1.write_bytes(b'x')
    c2 = amb_dir / 'c2.jpg'
    c2.write_bytes(b'x')
    _insert_file_row(str(root_dir), 'tracked_amb.jpg', 'h_ambiguous')
    _insert_conflict('h_ambiguous', str(c1))
    _insert_conflict('h_ambiguous', str(c2))

    # h_none: candidate doesn't exist on disk -> skipped.
    _insert_file_row(str(root_dir), 'tracked_none.jpg', 'h_none')
    _insert_conflict('h_none', str(root_dir / 'missing.jpg'))

    resp = client.post('/tracking/conflicts/resolve-batch', json={
        'strategy': 'use_candidate', 'md5_hashes': ['h_single', 'h_ambiguous', 'h_none'],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body['resolved'] == 1
    skipped_hashes = {s['md5_hash'] for s in body['skipped']}
    assert skipped_hashes == {'h_ambiguous', 'h_none'}

    with get_engine().connect() as conn:
        row = conn.execute(files_table.select().where(files_table.c.md5_hash == 'h_single')).fetchone()
        assert row.directory == str(single_dir)
        assert row.file_name == 'copy.jpg'
