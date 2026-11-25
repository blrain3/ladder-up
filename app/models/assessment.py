from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AssessmentTest(Base):
    __tablename__ = "assessment_test"
    __table_args__ = (Index("idx_test_date", "held_on"), {"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    test_type: Mapped[str] = mapped_column(Text)
    held_on: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class TestScore(Base):
    __test__ = False  # 避免 pytest 将模型类误收集为测试类
    __tablename__ = "test_score"
    __table_args__ = (
        Index("uk_score", "test_id", "member_id", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("assessment_test.id", name="fk_score_test"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", name="fk_score_member"))
    solved_count: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[str | None] = mapped_column(Text)
    # Integer revisions distinguish edits within the same clock tick.
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    kp_snapshot: Mapped[str | None] = mapped_column(Text)


class MemberAssessment(Base):
    __tablename__ = "member_assessment"
    __table_args__ = (
        Index("uk_member_assessment", "member_id", "term", unique=True),
        Index("idx_assess_term", "term"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", name="fk_assess_member"))
    term: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    in_watchlist: Mapped[int] = mapped_column(Integer, server_default="0")
    assessed_at: Mapped[str] = mapped_column(Text)
    remark: Mapped[str | None] = mapped_column(Text)


class JobRun(Base):
    __tablename__ = "job_run"
    __table_args__ = (
        Index("uk_job_run", "job_name", "biz_date", "term", unique=True),
        Index("idx_job_status", "status", "started_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(Text)
    biz_date: Mapped[str] = mapped_column(Text)
    term: Mapped[str] = mapped_column(Text, server_default="")
    status: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default="1")
    started_at: Mapped[str] = mapped_column(Text)
    lease_expires_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
