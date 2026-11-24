import json
import tarfile

from sqlalchemy import insert, update

from app.models import Attachment, Member
from app.models.base import local_time
from bin.backup import create_backup, sha256
from bin.restore_check import restore_check


def test_backup_and_restore(database_url, engine, tmp_path):
    uploads = tmp_path / "uploads"
    relative = "2026/11/photo.png"
    photo = uploads / relative
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"fixture")
    now = local_time()
    with engine.begin() as connection:
        member_id = connection.execute(insert(Member).returning(Member.id), {
            "student_no": "test0001", "name": "测试会员", "password_hash": "fixture",
            "member_type": "正式会员", "member_status": "正式会员", "created_at": now, "updated_at": now,
        }).scalar_one()
        attachment_id = connection.execute(insert(Attachment).returning(Attachment.id), {
            "object_key": relative, "owner_member_id": member_id, "biz_type": "member_photo",
            "biz_id": member_id, "mime": "image/png", "byte_size": photo.stat().st_size,
            "sha256": sha256(photo), "created_at": now,
        }).scalar_one()
        connection.execute(update(Member).where(Member.id == member_id).values(
            photo_attachment_id=attachment_id
        ))

    archive = create_backup(database_url, uploads, tmp_path / "backups")
    with tarfile.open(archive, "r:gz") as bundle:
        with bundle.extractfile("manifest.json") as stream:
            manifest = json.load(stream)
        assert manifest["migration"] == "0003"
        assert manifest["files"] == [{"path": f"uploads/{relative}", "sha256": sha256(photo)}]
        database = manifest["database"]
        assert sha256(archive.parent / database["path"]) == database["sha256"]
    assert restore_check(archive) == manifest["file_count"] == 1
