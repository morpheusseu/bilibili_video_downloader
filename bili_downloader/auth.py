"""Opt-in login persistence in current-user files; no OS keychain access."""

from dataclasses import dataclass, field

from bpi.session import parse_cookie

from . import local_store
from .sources import DownloadError

COOKIE_NAMES = {
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    # Preserve actual browser device cookies; never manufacture device identities.
    "buvid3",
    "buvid4",
    "buvid_fp",
    "b_nut",
    "_uuid",
}


@dataclass(frozen=True)
class Session:
    mid: int
    name: str
    cookie: str = field(repr=False)
    user_agent: str = ""
    # Presentation-only; the account image cache owns persistence, not the credential file.
    avatar: bytes = field(default=b"", repr=False, compare=False)


def normalized_cookie(value: str) -> str:
    if len(value) > 16384:
        raise DownloadError("Cookie 内容过长")
    try:
        pairs = parse_cookie(value)
    except ValueError:
        raise DownloadError("Cookie 格式无效") from None
    if not pairs.get("SESSDATA"):
        raise DownloadError("Cookie 缺少 SESSDATA")
    return "; ".join(f"{key}={val}" for key, val in pairs.items() if key in COOKIE_NAMES)


def _session(data):
    if type(data["mid"]) is not int or data["mid"] <= 0 or not isinstance(data["name"], str):
        raise ValueError("Invalid saved identity")
    agent = data["user_agent"]
    if not isinstance(agent, str) or any(c in agent for c in "\r\n"):
        raise ValueError("Invalid saved user agent")
    return Session(data["mid"], data["name"], normalized_cookie(data["cookie"]), agent)


def _account_path(root, account_id):
    folder = local_store.account_folder(root / "accounts", account_id)
    if root.is_symlink() or (root / "accounts").is_symlink() or folder.is_symlink():
        raise ValueError("Symlink credential directory")
    return folder / "session.json"


def _active_account(root):
    if root.is_symlink():
        raise ValueError("Symlink credential directory")
    active = local_store.read_json(root / "active-account.json")
    if active is None:
        return None
    account_id = active["mid"]
    if type(account_id) is not int or account_id <= 0:
        raise ValueError("Invalid active account")
    return account_id


def save_session(session: Session) -> None:
    try:
        data = {
            "mid": session.mid,
            "name": session.name,
            "cookie": normalized_cookie(session.cookie),
            "user_agent": session.user_agent,
        }
        _session(data)
        root = local_store.data_root()
        path = _account_path(root, session.mid)
        local_store.private_directory(root)
        local_store.private_directory(root / "accounts")
        local_store.write_json(path, data)
        # Publish the selected account only after its complete credential is durable.
        local_store.write_json(root / "active-account.json", {"mid": session.mid})
    except Exception:
        raise DownloadError(
            "无法保存本机登录文件；当前登录仍保留在本次内存中，请检查本机数据目录"
        ) from None


def load_session() -> Session | None:
    try:
        root = local_store.data_root()
        account_id = _active_account(root)
        if account_id is None:
            return None
        session = _session(local_store.read_json(_account_path(root, account_id)))
        if session.mid != account_id:
            raise ValueError("Saved account mismatch")
        return session
    except Exception:
        raise DownloadError(
            "无法读取本机保存的登录；可以重新扫码，不会读取旧钥匙串或 passport.json"
        ) from None


def forget_session(account_id: int | None = None) -> None:
    try:
        root = local_store.data_root()
        active = _active_account(root)
        target = active if account_id is None else account_id
        if target is None:
            return
        path = _account_path(root, target)
        if path.is_symlink():
            raise ValueError("Symlink credential file")
        path.unlink(missing_ok=True)
        if path.parent.exists():
            path.parent.rmdir()
        if active == target:
            (root / "active-account.json").unlink(missing_ok=True)
    except Exception:
        raise DownloadError("无法完整删除本机保存的登录，请检查本机数据目录") from None
