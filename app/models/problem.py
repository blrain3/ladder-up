import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Problem(Base):
    __tablename__ = "problem"
    __table_args__ = (
        Index(
            "platform_external_uk", "platform", "external_id", unique=True,
            sqlite_where=sa.text("platform IS NOT NULL AND external_id IS NOT NULL"),
        ),
        Index("idx_problem_difficulty", "difficulty"),
        Index("idx_problem_type", "problem_type"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    platform: Mapped[str | None] = mapped_column(Text)
    difficulty: Mapped[str] = mapped_column(Text)
    problem_type: Mapped[str] = mapped_column(Text)
    statement: Mapped[str | None] = mapped_column(Text)
    sample_in: Mapped[str | None] = mapped_column(Text)
    sample_out: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("member.id", name="fk_problem_creator"))
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[str | None] = mapped_column(Text)


class ProblemKp(Base):
    __tablename__ = "problem_kp"
    __table_args__ = (Index("idx_pk_kp", "kp_id"),)

    problem_id: Mapped[int] = mapped_column(
        ForeignKey("problem.id", name="fk_pk_problem"), primary_key=True
    )
    kp_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_point.id", name="fk_pk_kp"), primary_key=True
    )


class ProblemSolution(Base):
    __tablename__ = "problem_solution"
    __table_args__ = (
        Index("idx_solution_problem", "problem_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    problem_id: Mapped[int] = mapped_column(
        ForeignKey("problem.id", name="fk_solution_problem")
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("member.id", name="fk_solution_author")
    )
    content_md: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
