# infrastructure/database/session_factory.py
from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class SessionFactory:
    def __init__(
        self,
        database_url: str,
        admin_database_url: str,
        target_database_name: str,
        auto_create_database: bool,
    ) -> None:
        self._database_url = database_url
        self._admin_database_url = admin_database_url
        self._target_database_name = target_database_name
        self._auto_create_database = auto_create_database
        self._engine: Engine | None = None
        self._sessionmaker: sessionmaker[Session] | None = None
        self._ensure_database_and_engine()

    def _ensure_database_exists(self) -> None:
        admin_engine = create_engine(
            self._admin_database_url,
            future=True,
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=True,
        )
        exists_sql = text(
            "SELECT 1 FROM pg_database WHERE datname = :database_name"
        )
        with admin_engine.connect() as connection:
            exists = connection.execute(
                exists_sql,
                {"database_name": self._target_database_name},
            ).scalar()
            if not exists:
                safe_db_name = self._target_database_name.replace('"', '""')
                connection.execute(text(f'CREATE DATABASE "{safe_db_name}"'))
        admin_engine.dispose()

    def _ensure_database_and_engine(self) -> None:
        if self._engine is not None and self._sessionmaker is not None:
            return
        if self._auto_create_database:
            self._ensure_database_exists()
        self._engine = create_engine(
            self._database_url,
            future=True,
            pool_pre_ping=True,
        )
        self._sessionmaker = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            future=True,
        )

    @property
    def engine(self) -> Engine:
        assert self._engine is not None
        return self._engine

    def create_session(self) -> Session:
        self._ensure_database_and_engine()
        assert self._sessionmaker is not None
        return self._sessionmaker()
