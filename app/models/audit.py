from sqlalchemy import CheckConstraint, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("before_json IS NULL OR json_valid(before_json)", name="ck_audit_before_json"),
        CheckConstraint("after_json IS NULL OR json_valid(after_json)", name="ck_audit_after_json"),
        Index("idx_audit_actor", "actor_id"),
        Index("idx_audit_target", "target_type", "target_id"),
        Index("idx_audit_time", "created_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[int | None] = mapped_column(Integer)
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
