from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def make_engine(url):
    if url.startswith("sqlite:///") and ":memory:" not in url:
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def connect(dbapi, _):
            dbapi.isolation_level = None
            dbapi.execute("PRAGMA foreign_keys=ON")

        @event.listens_for(engine, "begin")
        def begin(conn):
            # Serialize local writes, including read/validate/write sequences.
            conn.exec_driver_sql("BEGIN IMMEDIATE")
    return engine


def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
