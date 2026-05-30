from sqlalchemy import create_engine

from env.environment import Environment

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            Environment().get_database_url(),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
    return _engine


def _make_insert(table):
    dialect = get_engine().dialect.name
    if dialect == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise ValueError(f"Unsupported database dialect: {dialect}")
    return insert(table)


def upsert(table, records: list[dict], conflict_cols: list[str]):
    """INSERT … ON CONFLICT (conflict_cols) DO UPDATE — überschreibt alle anderen Felder."""
    stmt = _make_insert(table).values(records)
    update_cols = {k: getattr(stmt.excluded, k)
                   for k in records[0] if k not in conflict_cols}
    return stmt.on_conflict_do_update(index_elements=conflict_cols, set_=update_cols)


def upsert_ignore(table, records: list[dict], conflict_cols: list[str]):
    """INSERT … ON CONFLICT DO NOTHING."""
    return _make_insert(table).values(records).on_conflict_do_nothing(
        index_elements=conflict_cols)
