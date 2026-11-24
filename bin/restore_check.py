import argparse
import json
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from sqlalchemy.exc import SQLAlchemyError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import make_engine
from bin.backup import sha256, snapshot_info


def restore_check(archive: Path):
    with TemporaryDirectory() as directory, tarfile.open(archive, "r:gz") as bundle:
        root = Path(directory)
        entries = {}
        for entry in bundle:
            path = PurePosixPath(entry.name)
            if (not entry.isfile() or path.is_absolute() or ".." in path.parts
                    or path.as_posix() != entry.name or entry.name in entries):
                raise ValueError(f"Invalid archive entry: {entry.name!r}")
            entries[entry.name] = entry

        with bundle.extractfile(entries["manifest.json"]) as source:
            manifest = json.load(source)
        database = manifest["database"]
        files = manifest["files"]
        db_name = database["path"]
        if Path(db_name).name != db_name or not db_name.endswith(".db"):
            raise ValueError("Invalid database filename")
        expected_paths = {item["path"] for item in files}
        if (manifest["file_count"] != len(files) or len(expected_paths) != len(files)
                or any(not path.startswith("uploads/") for path in expected_paths)
                or set(entries) != expected_paths | {db_name, "manifest.json"}):
            raise ValueError("Archive file count or list differs from manifest")

        for item in [database, *files]:
            target = root / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(entries[item["path"]]) as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            if sha256(target) != item["sha256"]:
                raise ValueError(f"SHA-256 mismatch: {item['path']}")

        engine = make_engine(f"sqlite:///{root / db_name}")
        try:
            with engine.connect() as connection:
                if connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() != ["ok"]:
                    raise ValueError("integrity_check failed")
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                    raise ValueError("foreign_key_check failed")
                migration, paths = snapshot_info(connection)
                if migration != manifest["migration"] or {f"uploads/{p}" for p in paths} != expected_paths:
                    raise ValueError("Database migration or attachment list differs from manifest")
        finally:
            engine.dispose()
        return len(files)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", nargs="?", type=Path)
    args = parser.parse_args()
    try:
        archive = args.archive or max(Path("backups").glob("app-*.tar.gz"), default=None)
        if archive is None:
            raise FileNotFoundError("No backup archive found")
        count = restore_check(archive)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, tarfile.TarError, SQLAlchemyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"PASS: integrity_check=ok, foreign_key_check=ok, files={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
