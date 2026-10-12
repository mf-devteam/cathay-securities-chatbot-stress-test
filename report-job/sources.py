"""讀寫 GCS（gs://bucket/prefix）或本機資料夾（本機模擬用），對外提供相同介面。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

CHUNK = 4 * 1024 * 1024


class Store:
    def list(self, sub: str) -> list[str]: ...
    def read_chunks(self, rel: str) -> Iterator[bytes]: ...
    def read_text(self, rel: str) -> str | None: ...
    def write(self, rel: str, data: bytes, content_type: str) -> str: ...


class LocalStore(Store):
    def __init__(self, root: str):
        self.root = Path(root)

    def list(self, sub: str) -> list[str]:
        d = self.root / sub
        if not d.is_dir():
            return []
        # 略過上傳中的 .part 暫存檔
        return sorted(f"{sub}/{p.name}" for p in d.iterdir() if p.is_file() and not p.name.endswith(".part"))

    def read_chunks(self, rel: str) -> Iterator[bytes]:
        with open(self.root / rel, "rb") as f:
            while chunk := f.read(CHUNK):
                yield chunk

    def read_text(self, rel: str) -> str | None:
        p = self.root / rel
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def write(self, rel: str, data: bytes, content_type: str) -> str:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        return str(p)


class GcsStore(Store):
    def __init__(self, uri: str):
        from google.cloud import storage  # 只有 GCS 模式需要

        path = uri[len("gs://"):]
        self.bucket_name, _, self.prefix = path.partition("/")
        self.prefix = self.prefix.strip("/")
        self.client = storage.Client()
        self.bucket = self.client.bucket(self.bucket_name)

    def _key(self, rel: str) -> str:
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def list(self, sub: str) -> list[str]:
        base = self._key(sub).rstrip("/") + "/"
        out = []
        for blob in self.client.list_blobs(self.bucket_name, prefix=base):
            name = blob.name[len(self.prefix) + 1:] if self.prefix else blob.name
            if "/" not in name[len(sub) + 1:]:
                out.append(name)
        return sorted(out)

    def read_chunks(self, rel: str) -> Iterator[bytes]:
        # 鎖定讀取當下的物件版本（generation）；壓測機讀取中途覆蓋上傳也不會讀到混合內容
        current = self.bucket.get_blob(self._key(rel))
        if current is None:
            return
        pinned = self.bucket.blob(self._key(rel), generation=current.generation)
        with pinned.open("rb", chunk_size=CHUNK) as f:
            while chunk := f.read(CHUNK):
                yield chunk

    def read_text(self, rel: str) -> str | None:
        blob = self.bucket.blob(self._key(rel))
        return blob.download_as_text() if blob.exists() else None

    def write(self, rel: str, data: bytes, content_type: str) -> str:
        blob = self.bucket.blob(self._key(rel))
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{self.bucket_name}/{self._key(rel)}"

    def console_url(self, rel: str) -> str:
        return f"https://console.cloud.google.com/storage/browser/_details/{self.bucket_name}/{self._key(rel)}"


def open_store(uri: str) -> Store:
    return GcsStore(uri) if uri.startswith("gs://") else LocalStore(uri)


def read_json(store: Store, rel: str) -> dict:
    text = store.read_text(rel)
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}
