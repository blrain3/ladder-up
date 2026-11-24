import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_env: str
    session_ttl_hours: int

    @classmethod
    def from_env(cls) -> "Settings":
        names = ("DATABASE_URL", "APP_ENV", "SESSION_TTL_HOURS")
        missing = [name for name in names if not os.environ.get(name, "").strip()]
        if missing:
            raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

        try:
            ttl = int(os.environ["SESSION_TTL_HOURS"])
            if ttl <= 0:
                raise ValueError
        except ValueError as exc:
            raise RuntimeError("SESSION_TTL_HOURS must be a positive integer") from exc

        return cls(os.environ["DATABASE_URL"], os.environ["APP_ENV"], ttl)
