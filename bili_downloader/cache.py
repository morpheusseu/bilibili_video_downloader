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
from urllib.parse import urlparse

from .local_store import account_folder, private_directory
from .sources import DownloadError

log = logging.getLogger(__name__)
MAX_MESSAGES = 500
MAX_THREADS = 50
MAX_IMAGE_BYTES = 20 * 1024 * 1024
THREAD_BYTES = 16 * 1024 * 1024
MAX_RECORD = 2 * 1024 * 1024
IMAGE_AGE = 30 * 86400
MESSAGE_AGE = 30 * 86400


def canonical_image_url(value):
    """One trusted identity for network reads, disk bytes and decoded pictures."""
    url = urlparse(value)
    host = url.hostname or ""
    if (
        url.scheme not in {"http", "https"}
        or url.username
        or url.password
        or url.netloc != host
        or not (host == "hdslb.com" or host.endswith(".hdslb.com"))
    ):
        return ""
    return url._replace(scheme="https").geturl()


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
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS images (key TEXT PRIMARY KEY, "
                "account_id INTEGER NOT NULL, "
                "body BLOB NOT NULL, touched REAL NOT NULL)"
            )
            self.connection.execute("CREATE INDEX IF NOT EXISTS images_touched ON images(touched)")
            with self.connection:
                self._import_base64_images()
                self._prune_images()
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

    def _import_base64_images(self):
        # One atomic conversion, not an alternate read path: after commit the
        # old representation is removed and only the BLOB table owns images.
        db = self._db()
        imported = 0
        for key, raw, touched in db.execute(
            "SELECT key, CASE WHEN length(value)<=? THEN value END, touched "
            "FROM records WHERE kind='image'",
            (MAX_RECORD,),
        ).fetchall():
            try:
                item = json.loads(raw)
                if (
                    item["account_id"] != self.account_id
                    or item["kind"] != "image"
                    or not isinstance(item["key"], str)
                    or self._key(item["key"]) != key
                ):
                    raise ValueError("Invalid image identity")
                body = base64.b64decode(item["value"], validate=True)
                if not body or len(body) > MAX_IMAGE_BYTES:
                    raise ValueError("Invalid image size")
            except (ValueError, TypeError, KeyError):
                log.warning("Discarded invalid former image cache entry")
                continue
            inserted = db.execute(
                "INSERT OR IGNORE INTO images VALUES (?,?,?,?)",
                (key, self.account_id, body, touched),
            )
            imported += inserted.rowcount
        db.execute("DELETE FROM records WHERE kind='image'")
        if imported:
            log.info("Image cache converted entries=%s", imported)

    def _prune_images(self):
        self._db().execute("DELETE FROM images WHERE touched<=?", (self.clock() - IMAGE_AGE,))

    def get(self, kind, key, *, max_age=MESSAGE_AGE):
        db = self._db()
        try:
            row = db.execute(
                "SELECT value, touched FROM records WHERE kind=? AND key=?",
                (kind, self._key(key)),
            ).fetchone()
            if row is None:
                return None
            if max_age is not None and self.clock() - row[1] > max_age:
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

    def put_many(self, values, *, remove=()):
        db = self._db()
        encoded = []
        for (kind, key), value in values.items():
            if kind == "image":
                raise DownloadError("图片只允许通过二进制图片缓存写入")
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
                    "DELETE FROM records WHERE kind=? AND key=?",
                    [(kind, self._key(key)) for kind, key in remove],
                )
                db.executemany(
                    "INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(kind,key) DO UPDATE SET "
                    "value=excluded.value, touched=excluded.touched",
                    encoded,
                )
                rows = db.execute(
                    "SELECT key, length(value) FROM records WHERE kind='thread' "
                    "ORDER BY touched DESC, rowid DESC"
                ).fetchall()
                size = 0
                for index, (key, length) in enumerate(rows):
                    size += length
                    if size > THREAD_BYTES or index >= MAX_THREADS:
                        db.execute("DELETE FROM records WHERE kind='thread' AND key=?", (key,))
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
        db = self._db()
        key, now = self._key(url), self.clock()
        try:
            with db:
                row = db.execute(
                    "SELECT account_id, CASE WHEN length(body)<=? THEN body END, touched "
                    "FROM images WHERE key=?",
                    (MAX_IMAGE_BYTES, key),
                ).fetchone()
                if row is None:
                    return None
                if row[0] != self.account_id:
                    raise ValueError("Invalid image account")
                if now - row[2] >= IMAGE_AGE:
                    db.execute("DELETE FROM images WHERE key=?", (key,))
                    return None
                body = row[1]
                if not isinstance(body, bytes) or not body:
                    raise ValueError("Invalid image body")
                db.execute("UPDATE images SET touched=? WHERE key=?", (now, key))
            log.info("Image cache reused bytes=%s", len(body))
            return body
        except (sqlite3.Error, ValueError, TypeError):
            raise DownloadError("图片缓存校验失败，未使用该缓存") from None

    def put_image(self, url, body):
        db = self._db()
        if not isinstance(body, bytes) or len(body) > MAX_IMAGE_BYTES:
            raise DownloadError("图片缓存超过 20 MiB 或不是二进制内容，未保存")
        if not body:
            return
        try:
            with db:
                db.execute(
                    "INSERT INTO images VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                    "account_id=excluded.account_id, body=excluded.body, touched=excluded.touched",
                    (self._key(url), self.account_id, body, self.clock()),
                )
                self._prune_images()
        except sqlite3.Error:
            raise DownloadError("图片缓存写入失败，未保存本次图片") from None

    def touch_images(self, urls):
        db = self._db()
        try:
            with db:
                db.executemany(
                    "UPDATE images SET touched=? WHERE key=? AND account_id=?",
                    [(self.clock(), self._key(url), self.account_id) for url in urls],
                )
                self._prune_images()
        except sqlite3.Error:
            raise DownloadError("图片缓存使用时间更新失败") from None

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
