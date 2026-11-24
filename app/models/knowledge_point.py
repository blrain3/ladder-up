import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class KnowledgePoint(Base):
    __tablename__ = "knowledge_point"
    __table_args__ = (
        # 同父下 name 唯一；根节点 parent_id 为 NULL，用 COALESCE(parent_id, 0)
        # 的表达式索引让 NULL 也参与唯一约束（等价于 0 哨兵方案）。
        Index("uk_kp_parent_name", sa.text("coalesce(parent_id, 0)"), "name", unique=True),
        Index("idx_kp_parent", "parent_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_point.id", name="fk_kp_parent")
    )
    name: Mapped[str] = mapped_column(Text)
    difficulty: Mapped[str] = mapped_column(Text, server_default="入门")
    depth: Mapped[int] = mapped_column(Integer, server_default="1")
    path: Mapped[str] = mapped_column(Text, server_default="")
    created_at: Mapped[str] = mapped_column(Text)
