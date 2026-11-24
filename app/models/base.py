from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def local_time() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
