from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Member(Base):
    __tablename__ = "member"
    __table_args__ = (
        Index("uk_member_student_no", "student_no", unique=True),
        Index("idx_member_status", "member_status"),
        Index("idx_member_role", "role"),
        Index("idx_member_dept", "dept_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_no: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    major: Mapped[str | None] = mapped_column(Text)
    grade: Mapped[int | None] = mapped_column(Integer)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    auth_provider: Mapped[str] = mapped_column(Text, server_default="local")
    photo_attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("attachment.id", name="fk_member_photo")
    )
    member_type: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, server_default="member")
    member_status: Mapped[str] = mapped_column(Text)
    dept_id: Mapped[int | None] = mapped_column(ForeignKey("department.id", name="fk_member_dept"))
    joined_on: Mapped[str | None] = mapped_column(Text)
    must_change_password: Mapped[int] = mapped_column(Integer, server_default="0")
    left_on: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[str | None] = mapped_column(Text)


class MemberApplication(Base):
    __tablename__ = "member_application"
    __table_args__ = (
        CheckConstraint("active_flag = '1'", name="ck_app_active_flag"),
        Index("idx_app_status", "status", "created_at"),
        Index("uk_app_active", "student_no", "apply_type", "active_flag", unique=True),
        Index("uk_app_receipt_code_hash", "receipt_code_hash", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_no: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    major: Mapped[str | None] = mapped_column(Text)
    grade: Mapped[int | None] = mapped_column(Integer)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    apply_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    active_flag: Mapped[str | None] = mapped_column(Text)
    receipt_code_hash: Mapped[str | None] = mapped_column(Text)
    interview_note: Mapped[str | None] = mapped_column(Text)
    interviewed_at: Mapped[str | None] = mapped_column(Text)
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("member.id", name="fk_app_reviewer"))
    reviewed_at: Mapped[str | None] = mapped_column(Text)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id", name="fk_app_member"))
    created_at: Mapped[str] = mapped_column(Text)
