"""The sole provider/session owner. All provider API calls go through bpi-py."""

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from bpi import AsyncBpiClient
from bpi.errors import ApiError, AuthenticationError, HttpStatusError, ResponseDecodeError
from bpi.session import parse_cookie

from .auth import Session, normalized_cookie
from .sources import DownloadError, Source, VideoPart


class RequestGate:
    """Shared by API and media transfers; completion-to-next-start spacing."""

    def __init__(self, minimum: float = 0.5, maximum: float = 1.5):
        if not 0 <= minimum <= maximum:
            raise ValueError("Invalid request spacing")
        self.minimum, self.maximum = minimum, maximum
        self._lock = asyncio.Lock()
        self._last_end = time.monotonic()
        self.stopped = False
        self.count = 0
        self.last_gap: float | None = None
        self.minimum_observed_gap: float | None = None

    @asynccontextmanager
    async def request(self):
        async with self._lock:
            if self.stopped:
                raise DownloadError("已因风控/限流停止请求；请到官方客户端检查账号后再重启应用")
            gap = random.uniform(self.minimum, self.maximum)
            await asyncio.sleep(max(0, self._last_end + gap - time.monotonic()))
            self.last_gap = time.monotonic() - self._last_end if self._last_end else None
            if self.last_gap is not None:
                self.minimum_observed_gap = min(
                    self.minimum_observed_gap
                    if self.minimum_observed_gap is not None
                    else self.last_gap,
                    self.last_gap,
                )
            self.count += 1
            try:
                yield
            finally:
                self._last_end = time.monotonic()

    def check(self, status: int, code=None):
        if status in {403, 412, 429} or code in {-352, -412}:
            self.stopped = True
            raise DownloadError("服务器拒绝请求或触发限流/风控，已停止；不会自动重试或绕过")


class ApiHttpClient(httpx.AsyncClient):
    def __init__(self, gate: RequestGate, **kwargs):
        super().__init__(timeout=30, trust_env=False, follow_redirects=False, **kwargs)
        self.gate = gate
        self.user_agent = ""

    async def send(self, request, **kwargs):
        if (
            request.method != "GET"
            or request.url.scheme != "https"
            or request.url.host
            not in {
                "api.bilibili.com",
                "passport.bilibili.com",
            }
        ):
            raise DownloadError("拒绝非只读或非受信 API 请求")
        async with self.gate.request():
            if self.user_agent:
                request.headers["User-Agent"] = self.user_agent
            response = await super().send(request, **kwargs)
            self.gate.check(response.status_code)
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                self.gate.check(response.status_code, payload.get("code"))
            return response


def safe_error(error: Exception) -> str:
    if isinstance(error, DownloadError):
        return str(error)
    if isinstance(error, AuthenticationError):
        return "登录已失效或此内容要求登录，请重新扫码"
    if isinstance(error, ApiError):
        return f"Bilibili API 返回错误 {error.code}（权限、内容状态或接口变化）；没有自动重试"
    if isinstance(error, HttpStatusError):
        return f"Bilibili API 返回 HTTP {error.status_code}；没有自动重试"
    if isinstance(error, ResponseDecodeError):
        return "Bilibili 响应与 bpi-py 数据模型不兼容；已停止，不会把空数据当作成功"
    if isinstance(error, (httpx.HTTPError, OSError)):
        return f"网络或文件操作失败（{type(error).__name__}）；未输出 Cookie 或下载地址"
    return f"操作失败（{type(error).__name__}）"


class Bilibili:
    def __init__(self, *, gate: RequestGate | None = None, transport=None):
        self.gate = gate or RequestGate()
        self.http = ApiHttpClient(self.gate, transport=transport)
        self.sdk = AsyncBpiClient(http_client=self.http)
        self.session: Session | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.clear_session()
        await self.sdk.aclose()
        await self.http.aclose()

    def clear_session(self):
        self.http.cookies.clear()
        self.http.user_agent = ""
        self.session = None

    async def use_cookie(self, cookie: str, *, user_agent: str = "") -> Session:
        self.clear_session()
        try:
            if any(c in user_agent for c in "\r\n"):
                raise DownloadError("无效的浏览器 User-Agent")
            self.http.user_agent = user_agent
            for name, value in parse_cookie(normalized_cookie(cookie)).items():
                self.http.cookies.set(name, value, domain=".bilibili.com", path="/")
            nav = await self.sdk.login.nav()
            if not nav.is_login or not nav.mid:
                raise DownloadError("登录身份验证失败")
            self.session = Session(
                nav.mid, nav.uname or str(nav.mid), normalized_cookie(cookie), user_agent
            )
            return self.session
        except BaseException:
            self.clear_session()
            raise

    async def parts(self, source: Source, *, all_parts=False, page=1) -> AsyncIterator[VideoPart]:
        if page < 1:
            raise DownloadError("分 P 从 1 开始")
        if source.kind == "episode":
            detail = await self.sdk.bangumi.detail(ep_id=int(source.key))
            episodes = list(detail.episodes)
            for section in detail.section or []:
                episodes.extend(section.episodes)
            episode = next((e for e in episodes if e.id == int(source.key)), None)
            if episode is None:
                raise DownloadError("番剧接口没有返回指定剧集")
            if episode.rights and (episode.rights.area_limit or not episode.rights.allow_download):
                raise DownloadError("此剧集不允许下载或存在地区限制，不会绕过")
            yield VideoPart(
                episode.bvid,
                episode.aid,
                episode.cid,
                detail.title,
                episode.long_title or episode.title,
                1,
                episode_id=episode.id,
            )
            return
        if source.kind in {"favorite", "user"}:
            if source.kind == "user":
                folders = await self.sdk.fav.created_list(up_mid=int(source.key))
                pattern = re.compile(source.pattern)
                folder_ids = [f.id for f in folders.list if pattern.search(f.title)]
                if not folder_ids:
                    raise DownloadError("没有匹配名称的收藏夹")
            else:
                folder_ids = [int(source.key)]
            seen = set()
            for folder_id in folder_ids:
                async for bvid in self.favorite_videos(folder_id):
                    if bvid not in seen:
                        seen.add(bvid)
                        async for part in self.parts(
                            Source("video", bvid), all_parts=all_parts, page=page
                        ):
                            yield part
            return
        if source.kind != "video":
            raise DownloadError("未知下载来源")
        identity = (
            {"aid": int(source.key[2:])}
            if source.key.lower().startswith("av")
            else {"bvid": source.key}
        )
        info = await self.sdk.video.view(**identity)
        pages = info.pages or await self.sdk.video.page_list(bvid=info.bvid)
        wanted = source.page or page
        selected = pages if all_parts else [p for p in pages if p.page == wanted]
        if not selected:
            raise DownloadError(f"视频没有第 {wanted} P")
        for part in selected:
            yield VideoPart(info.bvid, info.aid, part.cid, info.title, part.part, part.page)

    async def favorite_videos(self, folder_id: int) -> AsyncIterator[str]:
        page, seen = 1, set()
        while True:
            data = await self.sdk.fav.list_detail(media_id=folder_id, page=page, page_size=20)
            keys = {(v.type_name, v.id) for v in data.medias}
            if data.has_more and not (keys - seen):
                raise DownloadError("收藏夹分页未推进，已停止，不能保证列表完整")
            for media in data.medias:
                key = (media.type_name, media.id)
                bvid = media.bvid or media.bv_id
                if key not in seen and media.type_name == 2 and bvid:
                    yield bvid
                seen.add(key)
            if not data.has_more:
                return
            page += 1

    async def streams(self, part: VideoPart, quality: int):
        if part.episode_id is not None:
            data = await self.sdk.bangumi.video_stream(
                ep_id=part.episode_id, quality=quality, format_flags=16
            )
            if data.is_drm or data.is_preview:
                raise DownloadError("剧集为 DRM 内容或试看流，不会下载或绕过限制")
            return data
        return await self.sdk.video.play_url(
            bvid=part.bvid, cid=part.cid, quality=quality, format_flags=16, try_look=False
        )
