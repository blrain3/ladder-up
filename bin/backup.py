import hashlib
import json
import shutil
import sys
import tarfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from sqlite3 import Connection
from tempfile import TemporaryDirectory
from typing import cast
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings
from app.db import database_path, make_engine


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def attachment_path(object_key: str) -> PurePosixPath:
    key = PurePosixPath(object_key).as_posix()
    key = key.removeprefix("data/uploads/")
    path = PurePosixPath(key)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in key:
        raise ValueError(f"Invalid attachment path: {object_key!r}")
    return path


def snapshot_info(connection):
    migration = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    keys = connection.exec_driver_sql("SELECT object_key FROM attachment ORDER BY object_key").scalars()
    paths = [attachment_path(key).as_posix() for key in keys]
    if len(set(paths)) != len(paths):
        raise ValueError("Attachment paths overlap")
    return migration, sorted(paths)


def sqlite_snapshot(source_url: str, destination: Path):
    source = make_engine(source_url)
    target = make_engine(f"sqlite:///{destination}")
    try:
        with source.connect() as original, target.connect() as copy:
            original_db = cast(Connection, original.connection.driver_connection)
            target_db = cast(Connection, copy.connection.driver_connection)
            original_db.backup(target_db)
            return snapshot_info(copy)
    finally:
        source.dispose()
        target.dispose()


def create_backup(database_url: str, uploads: Path, backups: Path) -> Path:
    if not database_path(database_url).is_file():
        raise FileNotFoundError("SQLite database does not exist. Run make migrate first.")

    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%dT%H%M%S-%f")
    basename = f"app-{stamp}"
    uploads = uploads.resolve()

    with TemporaryDirectory(dir=backups) as directory:
        staging = Path(directory).resolve()
        database = staging / f"{basename}.db"
        migration, paths = sqlite_snapshot(database_url, database)
        manifest = {
            "migration": migration,
            "database": {"path": database.name, "sha256": sha256(database)},
            "file_count": len(paths),
            "files": [],
        }
        archive = staging / f"{basename}.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(database, arcname=database.name)
            for relative in paths:
                source = (uploads / relative).resolve(strict=True)
                if not source.is_relative_to(uploads) or not source.is_file():
                    raise ValueError(f"Invalid attachment source: {relative!r}")

                # Hash and archive the same bytes even if an upload changes concurrently.
                staged_file = staging / "attachment"
                with source.open("rb") as incoming, staged_file.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                archive_path = f"uploads/{relative}"
                manifest["files"].append({"path": archive_path, "sha256": sha256(staged_file)})
                bundle.add(staged_file, arcname=archive_path)

            manifest_file = staging / "manifest.json"
            manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            bundle.add(manifest_file, arcname="manifest.json")

        database.replace(backups / database.name)
        published = backups / archive.name
        archive.replace(published)
    return published


if __name__ == "__main__":
    result = create_backup(Settings.from_env().database_url, Path("data/uploads"), Path("backups"))
    print(f"Backup OK: {result}")
