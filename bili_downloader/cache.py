"""Account-scoped current-user cache. Provider state remains authoritative.

Each thread's received bodies and forward cursor are one atomic JSON record. No
credentials, send requests, or replayable actions are stored here.
"""

import base64
import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

from .local_store import account_folder, private_directory
from .sources import DownloadError

log = logging.getLogger(__name__)
MAX_MESSAGES = 500
MAX_THREADS = 50
MAX_IMAGES = 128
IMAGE_BYTES = 32 * 1024 * 1024
THREAD_BYTES = 16 * 1024 * 1024
MAX_RECORD = 2 * 1024 * 1024
IMAGE_AGE = 7 * 86400
MESSAGE_AGE = 30 * 86400


class AccountCache:
    def __init__(self, root, account_id, *, clock=time.time):
        self.account_id, self.clock = account_id, clock
        self.connection = None
        self.closed = False
        self.folder = account_folder(root, account_id)
        try:
            private_directory(root)
            private_directory(self.folder)
            path = self.folder / "cache.sqlite3"
            if path.is_symlink():
                raise ValueError("Symlink cache file")
            # Create with private permissions before SQLite writes its first page.
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
            self.connection = sqlite3.connect(path)
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA auto_vacuum=FULL")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, key TEXT NOT NULL, "
                "value BLOB NOT NULL, touched REAL NOT NULL, PRIMARY KEY(kind, key))"
            )
            self.connection.commit()
        except Exception:
            self.close()
            raise DownloadError("本机持久缓存无法打开，请检查数据目录权限") from None

    def _db(self):
        if self.closed or self.connection is None:
            raise DownloadError("账号缓存已关闭，未继续写入旧账号")
        return self.connection

    @staticmethod
    def _key(key):
        return hashlib.sha256(str(key).encode()).hexdigest()

    def get(self, kind, key):
        db = self._db()
        try:
            row = db.execute(
                "SELECT value, touched FROM records WHERE kind=? AND key=?",
                (kind, self._key(key)),
            ).fetchone()
            if row is None:
                return None
            age = IMAGE_AGE if kind == "image" else MESSAGE_AGE
            if self.clock() - row[1] > age:
                with db:
                    db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, self._key(key)))
                return None
            if len(row[0]) > MAX_RECORD:
                raise ValueError("Oversized cache record")
            envelope = json.loads(row[0])
            if (envelope["account_id"], envelope["kind"], envelope["key"]) != (
                self.account_id,
                kind,
                str(key),
            ):
                raise ValueError("Cache identity mismatch")
            return envelope["value"]
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            raise DownloadError("持久缓存校验失败，未使用损坏数据或推进消息游标") from None

    def put_many(self, values):
        db = self._db()
        encoded = []
        for (kind, key), value in values.items():
            raw = json.dumps(
                {"account_id": self.account_id, "kind": kind, "key": str(key), "value": value},
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            if len(raw) > MAX_RECORD:
                raise DownloadError("单项缓存超过大小上限，未提交缓存或推进消息游标")
            encoded.append((kind, self._key(key), raw, self.clock()))
        try:
            with db:
                db.executemany(
                    "INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET "
                    "value=excluded.value, touched=excluded.touched",
                    encoded,
                )
                for kind, count, budget in (
                    ("image", MAX_IMAGES, IMAGE_BYTES),
                    ("thread", MAX_THREADS, THREAD_BYTES),
                ):
                    rows = db.execute(
                        "SELECT key, length(value) FROM records WHERE kind=? "
                        "ORDER BY touched DESC, rowid DESC",
                        (kind,),
                    ).fetchall()
                    size = 0
                    for index, (key, length) in enumerate(rows):
                        size += length
                        if index >= count or size > budget:
                            db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, key))
        except sqlite3.Error:
            raise DownloadError("持久缓存写入失败，未提交本次消息或推进游标") from None

    def threads(self):
        db = self._db()
        try:
            rows = db.execute(
                "SELECT key FROM records WHERE kind='thread' ORDER BY touched ASC, rowid ASC"
            ).fetchall()
            result = []
            for (key,) in rows:
                row = db.execute(
                    "SELECT value, touched FROM records WHERE kind='thread' AND key=?", (key,)
                ).fetchone()
                if self.clock() - row[1] > MESSAGE_AGE:
                    continue
                if len(row[0]) > MAX_RECORD:
                    raise ValueError("Oversized cache record")
                payload = json.loads(row[0])
                peer = payload["key"]
                value = self.get("thread", peer)
                if value is not None:
                    result.append((int(peer), value))
            return result
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            raise DownloadError("持久消息缓存校验失败，未加载其游标") from None

    def image(self, url):
        value = self.get("image", url)
        if value is None:
            return None
        try:
            body = base64.b64decode(value, validate=True)
            if len(body) > 1024 * 1024:
                raise ValueError
            log.info("Image cache reused bytes=%s", len(body))
            return body
        except (ValueError, TypeError):
            raise DownloadError("图片缓存校验失败，未使用该缓存") from None

    def put_image(self, url, body):
        if body:
            self.put_many({("image", url): base64.b64encode(body).decode("ascii")})

    def close(self):
        self.closed = True
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    @classmethod
    def forget(cls, root, account_id):
        folder = account_folder(root, account_id)
        try:
            if Path(root).is_symlink() or folder.is_symlink():
                raise ValueError("Symlink cache directory")
            for name in (
                "cache.sqlite3",
                "cache.sqlite3-journal",
                "cache.sqlite3-wal",
                "cache.sqlite3-shm",
            ):
                path = folder / name
                if path.is_symlink():
                    raise ValueError("Symlink cache file")
                path.unlink(missing_ok=True)
            if folder.exists():
                folder.rmdir()
        except Exception:
            raise DownloadError("未能完整清理该账号缓存，请检查本机数据目录") from None
