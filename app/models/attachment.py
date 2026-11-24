from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Attachment(Base):
    __tablename__ = "attachment"
    __table_args__ = (
        Index("uk_attachment_key", "object_key", unique=True),
        Index("idx_attachment_owner", "owner_member_id"),
        Index("idx_attachment_biz", "biz_type", "biz_id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_key: Mapped[str] = mapped_column(Text)
    owner_member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id", name="fk_attachment_owner"))
    biz_type: Mapped[str] = mapped_column(Text)
    biz_id: Mapped[int | None] = mapped_column(Integer)
    mime: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[str | None] = mapped_column(Text)
