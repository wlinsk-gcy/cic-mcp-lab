"""Upload images to a public-read Aliyun OSS bucket and return public URLs."""
from __future__ import annotations

import mimetypes
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import quote

import alibabacloud_oss_v2 as oss

from ..config import Settings

KEY_PREFIX = "cic-mcp-lab"


def guess_image_type(filename: str) -> str:
    media_type, _ = mimetypes.guess_type(filename)
    if not media_type or not media_type.startswith("image/"):
        raise ValueError(f"Not an image file: {filename}")
    return media_type


def build_object_key(filename: str, today: date, unique: str) -> str:
    suffix = Path(filename).suffix.lower()
    return f"{KEY_PREFIX}/{today:%Y%m%d}/{unique}{suffix}"


def public_url(bucket: str, endpoint: str, key: str) -> str:
    return f"https://{bucket}.{endpoint}/{quote(key)}"


class OssUploader:
    def __init__(self, settings: Settings) -> None:
        cfg = oss.config.load_default()
        cfg.region = settings.oss_region
        cfg.endpoint = settings.oss_endpoint
        cfg.credentials_provider = oss.credentials.StaticCredentialsProvider(
            settings.oss_access_key_id,
            settings.oss_access_key_secret,
        )
        self._client = oss.Client(cfg)
        self._bucket = settings.oss_bucket
        self._endpoint = settings.oss_endpoint

    def upload_bytes(self, data: bytes, filename: str) -> str:
        content_type = guess_image_type(filename)
        key = build_object_key(filename, date.today(), uuid.uuid4().hex)
        self._client.put_object(
            oss.PutObjectRequest(
                bucket=self._bucket,
                key=key,
                body=data,
                content_type=content_type,
            )
        )
        return public_url(self._bucket, self._endpoint, key)

    def upload_file(self, path: Path) -> str:
        return self.upload_bytes(path.read_bytes(), path.name)
