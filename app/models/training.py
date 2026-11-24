from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

TASK_STATUSES = ("草稿", "已下发", "已结束")
TASK_ASSIGNEE_TYPES = ("全体会员", "指定会员")
CHECKIN_STATUSES = ("打卡", "补卡", "缺卡")
CHECKIN_SOURCES = ("手工", "OJ同步")


class TrainingPlan(Base):
    __tablename__ = "training_plan"
    __table_args__ = (
        Index("uk_plan_term", "term", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    term: Mapped[str] = mapped_column(Text)
    goal: Mapped[str | None] = mapped_column(Text)
    intensity: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class TrainingPhase(Base):
    __tablename__ = "training_phase"
    __table_args__ = (
        Index("idx_phase_plan", "plan_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("training_plan.id", name="fk_phase_plan"))
    name: Mapped[str] = mapped_column(Text)
    start_on: Mapped[str] = mapped_column(Text)
    end_on: Mapped[str] = mapped_column(Text)


class TrainingTask(Base):
    __tablename__ = "training_task"
    __table_args__ = (
        CheckConstraint("status IN ('草稿', '已下发', '已结束')", name="ck_task_status"),
        CheckConstraint(
            "assignee_type IN ('全体会员', '指定会员')", name="ck_task_assignee_type"
        ),
        Index("idx_task_phase", "phase_id"),
        Index("idx_task_deadline", "deadline_at"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phase_id: Mapped[int] = mapped_column(ForeignKey("training_phase.id", name="fk_task_phase"))
    title: Mapped[str] = mapped_column(Text)
    assignee_type: Mapped[str] = mapped_column(Text)
    deadline_at: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="草稿")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("member.id", name="fk_task_creator"))
    created_at: Mapped[str] = mapped_column(Text)


class TaskProblem(Base):
    __tablename__ = "task_problem"
    __table_args__ = (Index("idx_tp_problem", "problem_id"),)

    task_id: Mapped[int] = mapped_column(
        ForeignKey("training_task.id", name="fk_tp_task"), primary_key=True
    )
    problem_id: Mapped[int] = mapped_column(
        ForeignKey("problem.id", name="fk_tp_problem"), primary_key=True
    )
    sort_no: Mapped[int] = mapped_column(Integer, server_default="0")


class TaskAssignee(Base):
    __tablename__ = "task_assignee"
    __table_args__ = (
        CheckConstraint("assignee_type = '会员'", name="ck_assignee_type"),
        Index("idx_assignee_target", "assignee_type", "assignee_id"),
    )

    task_id: Mapped[int] = mapped_column(
        ForeignKey("training_task.id", name="fk_assignee_task"), primary_key=True
    )
    assignee_type: Mapped[str] = mapped_column(Text, primary_key=True)
    assignee_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class Checkin(Base):
    __tablename__ = "checkin"
    __table_args__ = (
        CheckConstraint("status IN ('打卡', '补卡', '缺卡')", name="ck_checkin_status"),
        CheckConstraint("source IN ('手工', 'OJ同步')", name="ck_checkin_source"),
        Index("uk_checkin", "member_id", "task_id", "checkin_date", unique=True),
        Index("idx_checkin_date", "checkin_date"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("training_task.id", name="fk_checkin_task"))
    member_id: Mapped[int] = mapped_column(ForeignKey("member.id", name="fk_checkin_member"))
    checkin_date: Mapped[str] = mapped_column(Text)
    minutes: Mapped[int | None] = mapped_column(Integer)
    solved_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)


class CheckinProblem(Base):
    __tablename__ = "checkin_problem"
    __table_args__ = (
        CheckConstraint("solved IN (0, 1)", name="ck_cp_solved"),
        Index("idx_cp_problem", "problem_id"),
    )

    checkin_id: Mapped[int] = mapped_column(
        ForeignKey("checkin.id", name="fk_cp_checkin"), primary_key=True
    )
    problem_id: Mapped[int] = mapped_column(
        ForeignKey("problem.id", name="fk_cp_problem"), primary_key=True
    )
    solved: Mapped[int] = mapped_column(Integer, server_default="0")
    attempts: Mapped[int | None] = mapped_column(Integer)
