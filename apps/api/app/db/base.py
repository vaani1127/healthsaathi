import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from e2d_core.ids import uuid7

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}


def str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """A text column with a CHECK constraint, easier to migrate than a native enum."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


def user_fk(*, nullable: bool = False, index: bool = False) -> Mapped[Any]:
    return mapped_column(ForeignKey("users.id"), nullable=nullable, index=index)


def clinic_fk(columns: list[str], target: str, target_columns: list[str] | None = None) -> Any:
    """Composite FK (clinic_id, x_id) -> target(clinic_id, id), so rows cannot cross clinics."""
    return ForeignKeyConstraint(
        ["clinic_id", *columns],
        [f"{target}.clinic_id", *(f"{target}.{c}" for c in (target_columns or ["id"]))],
    )


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ClinicScopedMixin(IdMixin):
    """Operational table: has clinic_id and an RLS policy. Use scoped_args() for __table_args__."""

    clinic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("clinics.id"), index=True)


def scoped_args(*args: Any) -> tuple[Any, ...]:
    """Table args for a clinic scoped table: adds the (clinic_id, id) key used by clinic_fk."""
    return (UniqueConstraint("clinic_id", "id"), *args)


class WorkflowMixin(CreatedAtMixin):
    """Workflow rows are explanation evidence and record who made them and when."""

    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
