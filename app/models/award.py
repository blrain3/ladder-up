from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class MemberAward(Base):
    __tablename__ = "member_award"
    __table_args__ = (
        CheckConstraint("award_type IN ('奖励', '惩处')", name="ck_award_type"),
        Index("idx_award_member", "member_id"),
        Index("uk_award_member_day_title", "member_id", "happened_on", "title", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", name="fk_award_member"))
    award_type: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    happened_on: Mapped[str] = mapped_column(Text)
    extra_points: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(Text)
