"""Simple storage for Telegram chat->group mapping.

If DATABASE_URL is provided, uses SQLAlchemy (PostgreSQL). Otherwise, falls back
to the in-memory/file-backed mapping in TelegramNotifier.state.chat_groups.
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine


class BaseGroupStore:
    def get_group(self, chat_id: int) -> str | None:  # pragma: no cover - interface
        raise NotImplementedError

    def set_group(self, chat_id: int, group_id: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    # Subscribers
    def get_subscribers(self) -> list[int]:  # pragma: no cover - interface
        raise NotImplementedError

    def add_subscriber(self, chat_id: int) -> bool:  # returns True if new
        raise NotImplementedError

    def remove_subscriber(self, chat_id: int) -> bool:  # returns True if existed
        raise NotImplementedError


class FileGroupStore(BaseGroupStore):
    def __init__(self, mapping: dict[str, str], subscribers: list[int]):
        self.mapping = mapping
        self.subscribers = subscribers

    def get_group(self, chat_id: int) -> str | None:
        return self.mapping.get(str(chat_id))

    def set_group(self, chat_id: int, group_id: str) -> None:
        self.mapping[str(chat_id)] = group_id

    # Subscribers
    def get_subscribers(self) -> list[int]:
        return list(self.subscribers)

    def add_subscriber(self, chat_id: int) -> bool:
        if chat_id not in self.subscribers:
            self.subscribers.append(chat_id)
            return True
        return False

    def remove_subscriber(self, chat_id: int) -> bool:
        if chat_id in self.subscribers:
            self.subscribers.remove(chat_id)
            return True
        return False


class SAGroupStore(BaseGroupStore):
    """SQLAlchemy-based PostgreSQL store."""

    def __init__(self, database_url: str):
        self.database_url = self._normalize_url(database_url)
        self.engine: Engine = create_engine(self.database_url, future=True, pool_pre_ping=True)
        self.meta = MetaData()
        self.tg_users = Table(
            "tg_users",
            self.meta,
            Column("chat_id", BigInteger, primary_key=True),
            Column("group_id", String, nullable=False),
            Column(
                "updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False
            ),
        )
        self.tg_subs = Table(
            "tg_subscribers",
            self.meta,
            Column("chat_id", BigInteger, primary_key=True),
            Column(
                "created_at", DateTime(timezone=True), server_default=func.now(), nullable=False
            ),
        )
        self._ensure_schema()

    @staticmethod
    def _normalize_url(url: str) -> str:
        # If driver not specified, default to pg8000 to avoid psycopg dependency
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://") and "+" not in url.split("://", 1)[1].split(":", 1)[0]:
            # e.g., postgresql://user:pass@host/db → postgresql+pg8000://...
            return url.replace("postgresql://", "postgresql+pg8000://", 1)
        return url

    def _ensure_schema(self) -> None:
        try:
            self.meta.create_all(self.engine, tables=[self.tg_users, self.tg_subs])
        except Exception:
            logger.exception("Не удалось создать таблицу tg_users")

    def get_group(self, chat_id: int) -> str | None:
        try:
            with self.engine.connect() as conn:
                stmt = select(self.tg_users.c.group_id).where(self.tg_users.c.chat_id == chat_id)
                row = conn.execute(stmt).fetchone()
                return row[0] if row else None
        except Exception:
            logger.exception("Не удалось прочитать группу из БД")
            return None

    def set_group(self, chat_id: int, group_id: str) -> None:
        try:
            with self.engine.begin() as conn:
                stmt = pg_insert(self.tg_users).values(chat_id=chat_id, group_id=group_id)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[self.tg_users.c.chat_id],
                    set_={"group_id": group_id, "updated_at": func.now()},
                )
                conn.execute(stmt)
        except Exception:
            logger.exception("Не удалось сохранить группу в БД")

    # Subscribers
    def get_subscribers(self) -> list[int]:
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(select(self.tg_subs.c.chat_id)).fetchall()
                return [int(r[0]) for r in rows]
        except Exception:
            logger.exception("Не удалось прочитать список подписчиков из БД")
            return []

    def add_subscriber(self, chat_id: int) -> bool:
        try:
            with self.engine.begin() as conn:
                stmt = pg_insert(self.tg_subs).values(chat_id=chat_id)
                stmt = stmt.on_conflict_do_nothing(index_elements=[self.tg_subs.c.chat_id])
                res = conn.execute(stmt)
                # SQLAlchemy doesn't expose rowcount reliably for DO NOTHING, so check existence
                existed = False
                if res.rowcount is not None and res.rowcount == 0:
                    existed = True
                return not existed
        except Exception:
            logger.exception("Не удалось добавить подписчика в БД")
            return False

    def remove_subscriber(self, chat_id: int) -> bool:
        try:
            from sqlalchemy import delete

            with self.engine.begin() as conn:
                res = conn.execute(delete(self.tg_subs).where(self.tg_subs.c.chat_id == chat_id))
                return (res.rowcount or 0) > 0
        except Exception:
            logger.exception("Не удалось удалить подписчика из БД")
            return False


def get_store(
    database_url: str | None, file_mapping: dict[str, str], subscribers: list[int]
) -> BaseGroupStore:
    if database_url:
        try:
            return SAGroupStore(database_url)
        except Exception:
            logger.exception("БД хранилище (SQLAlchemy) недоступно; используется файловое")
    return FileGroupStore(file_mapping, subscribers)
