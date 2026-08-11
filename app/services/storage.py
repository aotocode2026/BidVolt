"""StorageProvider：V1 本地磁盘适配器（租户前缀目录 + 不可变对象）。"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from pathlib import Path

from app.config import settings


class StorageProvider:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else Path(settings.storage_root)

    @staticmethod
    def sanitize_name(name: str) -> str:
        base = Path(name).name
        base = re.sub(r'[\\/:*?"<>|]', "_", base)
        return base.strip() or "unnamed"

    def _bucket(self, tenant_id: int) -> str:
        return f"enterprise_{tenant_id}"

    def save(self, data: bytes, tenant_id: int, original_name: str) -> dict:
        sha256 = hashlib.sha256(data).hexdigest()
        bucket = self._bucket(tenant_id)
        rel = f"{sha256}/original"  # 内容去重：同 sha256 同对象，原始名存 DB
        path = self.root / bucket / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return {"bucket": bucket, "object_key": rel, "sha256": sha256, "size_bytes": len(data)}

    def open(self, bucket: str, object_key: str) -> Path:
        base = (self.root / bucket).resolve()
        resolved = (self.root / bucket / object_key).resolve()
        if not str(resolved).startswith(str(base)):
            raise ValueError("非法的对象路径")
        if not resolved.exists():
            raise FileNotFoundError(resolved)
        return resolved

    def delete(self, bucket: str, object_key: str) -> None:
        try:
            self.open(bucket, object_key).unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def sign_download(file_id: int, tenant_id: int, expires_in: int = 300) -> str:
        """应用层签名下载 URL（V1，D16）：HMAC 签名 + 短时有效期。"""
        exp = int(time.time()) + expires_in
        payload = f"{file_id}:{tenant_id}:{exp}"
        sig = hmac.new(
            settings.jwt_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"/api/v1/files/{file_id}/signed?exp={exp}&sig={sig}"

    @staticmethod
    def verify_signed(file_id: int, tenant_id: int, exp: int, sig: str) -> bool:
        """校验签名与有效期。"""
        if int(time.time()) > int(exp):
            return False
        expected = hmac.new(
            settings.jwt_secret.encode("utf-8"),
            f"{file_id}:{tenant_id}:{exp}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, sig)
