"""Current-user local storage, not an encrypted credential vault."""

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


def data_root():
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "Bilibili Downloader" / "private"


def account_folder(root, account_id):
    if type(account_id) is not int or account_id <= 0:
        raise ValueError("Invalid account identity")
    return Path(root) / hashlib.sha256(str(account_id).encode()).hexdigest()


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Symlink data directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def read_json(path):
    if path.is_symlink():
        raise ValueError("Symlink data file")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Not a regular data file")
        if os.name == "posix":
            os.fchmod(stream.fileno(), 0o600)
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Oversized data file")
    return json.loads(raw)


def write_json(path, value):
    private_directory(path.parent)
    if path.is_symlink():
        raise ValueError("Symlink data file")
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > 65536:
        raise ValueError("Oversized data file")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
