import os
from base64 import b64decode
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Attachment, AuditLog, Member, MemberPermission, PermissionPoint

PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGNknVbBgA0wYRUdtBIA20cBI1mXgH4AAAAASUVORK5CYII="
)


def login(client, student_no, password):
    response = client.post("/auth/login", json={"student_no": student_no, "password": password})
    assert response.status_code == 303


def login_admin(client, admin):
    login(client, admin["student_no"], admin["password"])
    return client.get("/me").json()["data"]["csrf_token"]


def upload_photo(client, member_id, csrf, content, filename="photo.png", mime="image/png"):
    return client.post(
        f"/members/{member_id}/photo",
        files={"file": (filename, content, mime)}, data={"csrf_token": csrf},
    )


def test_png_upload_succeeds_and_backfills_photo(client, admin, accounts, engine):
    member_id = accounts[0]["student01"]
    csrf = login_admin(client, admin)
    response = upload_photo(client, member_id, csrf, PNG)
    assert response.status_code == 200
    attachment_id = response.json()["data"]["attachmentId"]

    with SessionLocal(bind=engine) as session:
        member = session.get(Member, member_id)
        assert member.photo_attachment_id == attachment_id
        attachment = session.get(Attachment, attachment_id)
        assert attachment.byte_size == len(PNG)
        assert attachment.sha256 == sha256(PNG).hexdigest()
        assert attachment.mime == "image/png"
        assert attachment.biz_type == "member_photo"
        assert attachment.biz_id == member_id
        assert attachment.owner_member_id == member_id
        object_key = attachment.object_key

    uploads_root = Path(os.environ["UPLOADS_DIR"])
    assert (uploads_root / object_key.removeprefix("data/uploads/")).read_bytes() == PNG


def test_fake_png_is_rejected(client, admin, accounts):
    member_id = accounts[0]["student01"]
    csrf = login_admin(client, admin)
    response = upload_photo(client, member_id, csrf, b"plain text", filename="fake.png")
    assert response.status_code == 400
    assert response.json()["code"] == 40001


def test_oversize_upload_is_rejected(client, admin, accounts):
    member_id = accounts[0]["student01"]
    csrf = login_admin(client, admin)
    oversize = b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024)
    response = upload_photo(client, member_id, csrf, oversize)
    assert response.status_code == 400
    assert response.json()["code"] == 40001


def test_photo_visibility_owner_and_others(client, client_factory, admin, accounts):
    member_id = accounts[0]["student01"]
    csrf = login_admin(client, admin)
    attachment_id = upload_photo(client, member_id, csrf, PNG).json()["data"]["attachmentId"]

    other = client_factory()
    login(other, "student02", accounts[1])
    response = other.get(f"/files/{attachment_id}")
    assert response.status_code == 403
    assert response.json()["code"] == 40301

    owner = client_factory()
    login(owner, "student01", accounts[1])
    response = owner.get(f"/files/{attachment_id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=60"
    assert response.headers["content-disposition"] == "inline"


@pytest.mark.parametrize("content", [PNG[:-1], PNG[:35]])
def test_truncated_png_is_rejected(client, signed_in, accounts, content):
    csrf = signed_in(client)
    response = upload_photo(client, accounts[0]["student01"], csrf, content)
    assert response.status_code == 400
    assert response.json()["code"] == 40001


def test_department_photo_scope_and_denial_audit(client, client_factory, signed_in, accounts, engine):
    ids, _ = accounts
    photos = {}
    for name in ("student01", "student02"):
        owner = client_factory()
        own_csrf = signed_in(owner, name)
        response = upload_photo(owner, ids[name], own_csrf, PNG)
        assert response.status_code == 200
        photos[name] = response.json()["data"]["attachmentId"]

    csrf = signed_in(client, "deptadmin")
    assert client.get(f"/files/{photos['student01']}").status_code == 200
    assert upload_photo(client, ids["student01"], csrf, PNG).status_code == 200
    assert client.post(
        f"/members/{ids['student01']}/photo/delete", data={"csrf_token": csrf},
    ).status_code == 200

    denied = [
        (client.get(f"/files/{photos['student02']}"), "attachment", photos["student02"]),
        (upload_photo(client, ids["student02"], csrf, PNG), "member", ids["student02"]),
        (client.post(f"/members/{ids['student02']}/photo/delete", data={"csrf_token": csrf}),
         "member", ids["student02"]),
    ]
    with SessionLocal(bind=engine) as session:
        for response, target_type, target_id in denied:
            assert response.status_code == 403
            event = session.scalars(select(AuditLog).where(
                AuditLog.request_id == response.headers["X-Request-ID"],
            )).one()
            assert (event.action, event.actor_id, event.target_type, event.target_id) == (
                "auth.permission.denied", ids["deptadmin"], target_type, target_id,
            )


def test_read_grant_does_not_allow_photo_writes(client, client_factory, signed_in, accounts, engine):
    from app.models.base import local_time

    ids, _ = accounts
    csrf = signed_in(client)
    attachment_id = upload_photo(client, ids["student01"], csrf, PNG).json()["data"]["attachmentId"]
    with SessionLocal(bind=engine) as session, session.begin():
        permission = PermissionPoint(code="member:read", module="A", description="查看会员")
        session.add(permission)
        session.flush()
        session.add(MemberPermission(
            member_id=ids["student02"], permission_id=permission.id, granted_at=local_time(),
        ))

    reader = client_factory()
    csrf = signed_in(reader, "student02")
    assert reader.get(f"/files/{attachment_id}").status_code == 200
    assert upload_photo(reader, ids["student01"], csrf, PNG).status_code == 403
    assert reader.post(
        f"/members/{ids['student01']}/photo/delete", data={"csrf_token": csrf},
    ).status_code == 403


def test_photo_commit_failure_leaves_no_files(client, signed_in, accounts, engine, monkeypatch):
    from sqlalchemy.orm import Session

    csrf = signed_in(client)
    member_id = accounts[0]["student01"]

    def fail_commit(_session):
        uploads = Path(os.environ["UPLOADS_DIR"])
        assert sum(path.is_file() for path in uploads.rglob("*")) == 1
        raise RuntimeError("simulated commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            upload_photo(client, member_id, csrf, PNG)
    assert sum(path.is_file() for path in Path(os.environ["UPLOADS_DIR"]).rglob("*")) == 0
    with SessionLocal(bind=engine) as session:
        assert session.scalar(select(Attachment)) is None
        assert session.get(Member, member_id).photo_attachment_id is None
