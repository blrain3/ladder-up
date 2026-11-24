from sqlalchemy import Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Department(Base):
    __tablename__ = "department"
    __table_args__ = (Index("uk_dept_name", "name", unique=True), {"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)


class SysConfig(Base):
    __tablename__ = "sys_config"

    config_key: Mapped[str] = mapped_column(Text, primary_key=True)
    config_value: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
