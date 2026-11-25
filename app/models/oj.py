from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class OjAccount(Base):
    __tablename__ = "oj_account"
    __table_args__ = (
        Index("uk_oj_account", "platform", "handle", unique=True),
        Index("uk_oj_member_platform", "member_id", "platform", unique=True),
        Index("idx_oj_member", "member_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", name="fk_oj_member"))
    platform: Mapped[str] = mapped_column(Text)
    handle: Mapped[str] = mapped_column(Text(collation="NOCASE"))
    verified_at: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class OjSubmission(Base):
    __tablename__ = "oj_submission"
    __table_args__ = (
        Index("uk_oj_submission", "platform", "external_id", unique=True),
        Index("idx_sub_time", "submitted_at"),
        Index("idx_sub_account", "oj_account_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    oj_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("oj_account.id", name="fk_sub_account", ondelete="SET NULL"),
    )
    problem_id: Mapped[int | None] = mapped_column(ForeignKey("problem.id", name="fk_sub_problem"))
    verdict: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[str] = mapped_column(Text)
    synced_at: Mapped[str] = mapped_column(Text)
