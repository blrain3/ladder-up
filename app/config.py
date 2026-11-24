import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_env: str
    session_ttl_hours: int
    uploads_dir: str
    cookie_secure: bool

    @classmethod
    def from_env(cls) -> "Settings":
        names = ("DATABASE_URL", "APP_ENV")
        missing = [name for name in names if not os.environ.get(name, "").strip()]
        if missing:
            raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

        try:
            ttl = int(os.environ.get("SESSION_TTL_HOURS", "8"))
            if ttl <= 0:
                raise ValueError
        except ValueError as exc:
            raise RuntimeError("SESSION_TTL_HOURS must be a positive integer") from exc

        uploads_dir = os.environ.get("UPLOADS_DIR", "data/uploads").strip() or "data/uploads"
        app_env = os.environ["APP_ENV"]
        cookie_secure = os.environ.get("COOKIE_SECURE", str(app_env == "production")).strip().lower()
        if cookie_secure not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise RuntimeError("COOKIE_SECURE must be a boolean")
        secure = cookie_secure in {"1", "true", "yes", "on"}
        return cls(os.environ["DATABASE_URL"], app_env, ttl, uploads_dir, secure)
