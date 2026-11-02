"""Verified provider identity and browser authorization material; no persistence."""

from dataclasses import dataclass, field

from bpi.session import parse_cookie

from .sources import DownloadError

COOKIE_NAMES = {
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
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
