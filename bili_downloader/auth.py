"""Native OS credential storage, controlled by the login dialog. No plaintext fallback."""

import json
import sys
from dataclasses import dataclass, field

from bpi.session import parse_cookie

from .sources import DownloadError

SERVICE = "bilibili-video-downloader.bpi-py"
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
    # Presentation-only, fetched once for each verified login; never stored in Keychain.
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


def _backend():
    # Select native encrypted storage directly; never use a plaintext fallback backend.
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    from keyring.backends.SecretService import Keyring

    return Keyring()


def save_session(session: Session) -> None:
    try:
        _backend().set_password(
            SERVICE,
            "default",
            json.dumps(
                {
                    "mid": session.mid,
                    "name": session.name,
                    "cookie": normalized_cookie(session.cookie),
                    "user_agent": session.user_agent,
                }
            ),
        )
    except Exception:
        raise DownloadError(
            "无法写入系统钥匙串；登录仍保留在本次内存中，没有保存明文凭证"
        ) from None


def load_session() -> Session | None:
    try:
        raw = _backend().get_password(SERVICE, "default")
        if raw is None:
            return None
        data = json.loads(raw)
        if type(data["mid"]) is not int or data["mid"] <= 0 or not isinstance(data["name"], str):
            raise ValueError
        agent = data["user_agent"]
        if not isinstance(agent, str) or any(c in agent for c in "\r\n"):
            raise ValueError
        return Session(data["mid"], data["name"], normalized_cookie(data["cookie"]), agent)
    except Exception:
        raise DownloadError(
            "无法读取系统钥匙串中的登录；可以重新扫码，不会读取旧 passport.json"
        ) from None


def forget_session() -> None:
    try:
        backend = _backend()
        if backend.get_password(SERVICE, "default") is not None:
            backend.delete_password(SERVICE, "default")
    except Exception:
        raise DownloadError("无法删除系统钥匙串中的登录，请在系统凭据管理器中处理") from None
