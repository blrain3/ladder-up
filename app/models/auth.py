from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PermissionPoint(Base):
    __tablename__ = "permission_point"
    __table_args__ = (Index("uk_perm_code", "code", unique=True), {"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text)
    module: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)


class MemberPermission(Base):
    __tablename__ = "member_permission"
    __table_args__ = (
        Index("uk_member_perm", "member_id", "permission_id", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", name="fk_mp_member"))
    permission_id: Mapped[int] = mapped_column(ForeignKey("permission_point.id", name="fk_mp_perm"))
    granted_at: Mapped[str] = mapped_column(Text)
    granted_by: Mapped[int | None] = mapped_column(Integer)


class MemberSession(Base):
    __tablename__ = "member_session"
    __table_args__ = (
        Index("uk_session_token", "token_hash", unique=True),
        Index("idx_session_member", "member_id", "expires_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", name="fk_session_member"))
    token_hash: Mapped[str] = mapped_column(Text)
    csrf_nonce: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[str] = mapped_column(Text)
    revoked_at: Mapped[str | None] = mapped_column(Text)
    revoke_reason: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
