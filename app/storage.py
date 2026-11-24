import hashlib
import struct
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from fastapi import HTTPException
from PIL import Image
from starlette.datastructures import UploadFile

from .models import Attachment
from .models.base import local_time

ALLOWED_MIME = {"image/jpeg": "jpg", "image/png": "png"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
CHUNK_BYTES = 64 * 1024


def sniff_image_type(head: bytes):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def upload_relative_path(object_key: str) -> PurePosixPath:
    prefix = "data/uploads/"
    if not object_key.startswith(prefix):
        raise ValueError(f"Invalid attachment key: {object_key!r}")
    relative = PurePosixPath(object_key[len(prefix):])
    if relative.is_absolute() or not relative.parts or ".." in relative.parts or "\\" in object_key:
        raise ValueError(f"Invalid attachment key: {object_key!r}")
    return relative


def resolve_upload_path(object_key: str, uploads_root: Path) -> Path:
    target = (uploads_root / upload_relative_path(object_key)).resolve()
    if not target.is_relative_to(uploads_root.resolve()):
        raise ValueError(f"Attachment escapes uploads directory: {object_key!r}")
    return target


def image_error():
    return HTTPException(400, "文件内容与图片格式不符")


@contextmanager
def stage_photo(file: UploadFile, owner_id: int, uploads_root: Path):
    mime = file.content_type
    if mime not in ALLOWED_MIME:
        raise HTTPException(400, "只允许上传 JPEG 或 PNG 图片")
    key = uuid.uuid4().hex
    staging = uploads_root / ".tmp" / key
    staging.parent.mkdir(parents=True, exist_ok=True)
    now = local_time()
    object_key = f"data/uploads/{now[:4]}/{now[5:7]}/{key}.{ALLOWED_MIME[mime]}"
    final_path = resolve_upload_path(object_key, uploads_root)

    try:
        size = 0
        digest = hashlib.sha256()
        file.file.seek(0)
        with staging.open("xb") as output:
            while chunk := file.file.read(CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(400, "文件不能超过 5 MB")
                digest.update(chunk)
                output.write(chunk)

        with staging.open("rb") as saved:
            if (staging.stat().st_size != size
                    or hashlib.file_digest(saved, "sha256").digest() != digest.digest()):
                raise OSError("Uploaded file checksum or size mismatch")
            saved.seek(0)
            if sniff_image_type(saved.read(8)) != mime:
                raise image_error()
            if mime == "image/png":
                if size < 20:
                    raise image_error()
                saved.seek(-12, 2)
                if saved.read(12) != b"\x00\x00\x00\x00IEND\xaeB`\x82":
                    raise image_error()

        try:
            with Image.open(staging) as image:
                image.verify()
            with Image.open(staging) as image:
                image.load()
        except (OSError, ValueError, SyntaxError, EOFError, struct.error, Image.DecompressionBombError):
            raise image_error() from None

        attachment = Attachment(
            object_key=object_key, owner_member_id=owner_id, biz_type="member_photo", biz_id=owner_id,
            mime=mime, byte_size=size, sha256=digest.hexdigest(), created_at=now,
        )
        yield attachment, staging, final_path
    except BaseException:
        final_path.unlink(missing_ok=True)
        raise
    finally:
        staging.unlink(missing_ok=True)
