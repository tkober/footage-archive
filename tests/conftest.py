import os

import pytest
import sqlalchemy
from sqlalchemy import text

TEST_DB_NAME = 'footage_archive_test'
PG_HOST = 'localhost'
PG_PORT = '5432'
PG_SUPERUSER = 'dev'
PG_SUPERUSER_PASSWORD = 'dev'  # auth is "trust" locally — any password works


def _postgres_reachable() -> bool:
    try:
        engine = sqlalchemy.create_engine(
            f'postgresql://{PG_SUPERUSER}:{PG_SUPERUSER_PASSWORD}@{PG_HOST}:{PG_PORT}/postgres'
        )
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        engine.dispose()
        return True
    except Exception:
        return False


def _ensure_test_database_exists() -> None:
    engine = sqlalchemy.create_engine(
        f'postgresql://{PG_SUPERUSER}:{PG_SUPERUSER_PASSWORD}@{PG_HOST}:{PG_PORT}/postgres'
    )
    with engine.connect() as conn:
        conn.execute(text('COMMIT'))  # CREATE DATABASE can't run inside a transaction
        exists = conn.execute(
            text('SELECT 1 FROM pg_database WHERE datname = :name'),
            {'name': TEST_DB_NAME},
        ).fetchone()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    engine.dispose()


@pytest.fixture(scope='session')
def _postgres_test_database():
    """Session-wide setup used ONLY by tests that actually need the DB (via
    the `db` fixture below) — tests that don't request it are unaffected by
    Postgres's availability. Skips cleanly if Postgres isn't reachable;
    otherwise creates footage_archive_test (if missing), points the DB_*
    env vars at it (DB_USER/DB_OWNER_USER = dev, trust auth), resets the
    engine singleton, and runs every migration once."""
    if not _postgres_reachable():
        pytest.skip('Postgres is not reachable on localhost:5432 — skipping DB-backed tests')

    _ensure_test_database_exists()

    # These must win over whatever .env (loaded by app.py / Environment) set,
    # since tests run against a dedicated throwaway database.
    os.environ['DB_URL'] = f'postgresql://{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}'
    os.environ['DB_USER'] = PG_SUPERUSER
    os.environ['DB_PASSWORD'] = PG_SUPERUSER_PASSWORD
    os.environ['DB_OWNER_USER'] = PG_SUPERUSER
    os.environ['DB_OWNER_PASSWORD'] = PG_SUPERUSER_PASSWORD

    import db.engine as engine_module
    engine_module._engine = None

    from alembic import command
    from alembic.config import Config
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    alembic_cfg = Config(os.path.join(repo_root, 'alembic.ini'))
    alembic_cfg.set_main_option('script_location', os.path.join(repo_root, 'alembic'))
    command.upgrade(alembic_cfg, 'head')

    yield

    engine_module.get_engine().dispose()


@pytest.fixture
def db(_postgres_test_database):
    """Request this fixture in any test that touches the database. Ensures
    the dedicated test database + schema exist, and TRUNCATEs every table
    after the test so each test starts from a clean slate."""
    import db.engine as engine_module
    from db.database import Database

    yield Database()

    from db.models import metadata
    with engine_module.get_engine().begin() as conn:
        table_names = ', '.join(f'"{t.name}"' for t in metadata.tables.values())
        conn.execute(text(f'TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE'))


@pytest.fixture
def root_dir(tmp_path, monkeypatch):
    """Point ROOT_DIR at a fresh tmp_path for the duration of a test — the
    Environment class reads the env var fresh on every call, no caching."""
    resolved = tmp_path.resolve()
    monkeypatch.setenv('ROOT_DIR', str(resolved))
    # alembic/env.py's load_dotenv() copies the developer's .env into
    # os.environ; tests rely on the code defaults unless they set these.
    monkeypatch.delenv('BROWSER_HIDDEN_EXTENSIONS', raising=False)
    monkeypatch.delenv('BROWSER_HIDDEN_NAMES', raising=False)
    return resolved
