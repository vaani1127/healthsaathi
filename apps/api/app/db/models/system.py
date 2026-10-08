import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, IdMixin, user_fk


class Job(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_pending", "run_after", postgresql_where=text("done_at IS NULL")),
    )

    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())
    attempts: Mapped[int] = mapped_column(server_default="0")
    locked_at: Mapped[datetime | None]
    done_at: Mapped[datetime | None]
    error: Mapped[str | None] = mapped_column(Text)


class PushSubscription(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "push_subscriptions"

    user_id: Mapped[uuid.UUID] = user_fk(index=True)
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
