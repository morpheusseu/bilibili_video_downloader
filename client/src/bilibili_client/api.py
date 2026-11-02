"""Session owner: bpi-py HTTP acquisition plus independent Broadcast routing hints."""

import asyncio
import logging
import random
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from bpi import AsyncBpiClient
from bpi.errors import (
    ApiError,
    AuthenticationError,
    HttpStatusError,
    ResponseDecodeError,
    TransportError,
)
from bpi.session import parse_cookie

from .account_content import AccountContent
from .broadcast import BroadcastClient, BroadcastState, Wakeup
from .cache import MAX_IMAGE_BYTES, AccountCache, canonical_image_url
from .creator_content import CreatorContent
from .following_collection import FollowingCollection
from .inbox import Inbox
from .library import FavoriteLibrary
from .session import Session, normalized_cookie
from .sources import DownloadError, Source, VideoPart

log = logging.getLogger(__name__)

# Only fixed endpoint names belong in diagnostics. Never log a URL, query,
# provider-controlled path segment, headers, payload or exception text here.
LOGGED_API_PATHS = frozenset(
    {
        "/x/web-interface/nav",
        "/x/web-interface/view",
        "/x/player/wbi/playurl",
        "/pgc/view/web/season",
        "/pgc/player/web/playurl",
        "/x/v3/fav/folder/created/list-all",
        "/x/v3/fav/resource/ids",
        "/x/v3/fav/resource/list",
        "/x/v3/fav/resource/infos",
        "/x/polymer/web-dynamic/v1/feed/all",
        "/x/polymer/web-dynamic/v1/feed/all/update",
        "/x/relation/followings",
        "/x/space/wbi/arc/search",
        "/x/space/like/video",
        "/x/space/coin/video",
        "/x/polymer/web-dynamic/v1/feed/space",
        "/x/web-interface/archive/relation",
        "/x/web-interface/history/cursor",
        "/session_svr/v1/session_svr/get_sessions",
        "/svr_sync/v1/svr_sync/fetch_session_msgs",
        "/account/v1/user/cards",
        "/web_im/v1/web_im/send_msg",
    }
)


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

    def check(self, status: int, code=None, *, provider_risk=False):
        if status in {403, 412, 429} or code in {-352, -412, -509, -799} or provider_risk:
            self.stopped = True
            raise DownloadError("服务器拒绝请求或触发限流/风控，已停止；不会自动重试或绕过")


class ApiHttpClient(httpx.AsyncClient):
    def __init__(self, gate: RequestGate, **kwargs):
        super().__init__(timeout=30, trust_env=False, follow_redirects=False, **kwargs)
        self.gate = gate
        self.user_agent = ""

    async def send(self, request, **kwargs):
        private_send = (
            request.method == "POST"
            and request.url.host == "api.vc.bilibili.com"
            and request.url.path == "/web_im/v1/web_im/send_msg"
        )
        if (
            (request.method != "GET" and not private_send)
            or request.url.scheme != "https"
            or request.url.host
            not in {
                "api.bilibili.com",
                "passport.bilibili.com",
                "api.vc.bilibili.com",
            }
            or (
                request.url.host == "api.vc.bilibili.com"
                and not private_send
                and request.url.path
                not in {
                    "/session_svr/v1/session_svr/get_sessions",
                    "/svr_sync/v1/svr_sync/fetch_session_msgs",
                    "/account/v1/user/cards",
                }
            )
        ):
            raise DownloadError("拒绝未获支持或非受信 API 请求；写操作仅允许显式私信发送")
        async with self.gate.request():
            started = time.monotonic()
            status, code, size, outcome = 0, None, 0, "error"
            try:
                if self.user_agent:
                    request.headers["User-Agent"] = self.user_agent
                response = await super().send(request, **kwargs)
                status, size = response.status_code, len(response.content)
                self.gate.check(status)
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    code = payload.get("code")
                    data = payload.get("data")
                    self.gate.check(
                        status,
                        code,
                        provider_risk=request.url.path == "/x/space/wbi/arc/search"
                        and isinstance(data, dict)
                        and data.get("is_risk") is True,
                    )
                outcome = "complete"
                return response
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            finally:
                log.info(
                    "HTTP request=%s endpoint=%s method=%s status=%s api_code=%s "
                    "outcome=%s duration_ms=%s gap_ms=%s body_bytes=%s",
                    self.gate.count,
                    request.url.path if request.url.path in LOGGED_API_PATHS else "other_api",
                    request.method,
                    status,
                    code if type(code) is int else "none",
                    outcome,
                    round((time.monotonic() - started) * 1000),
                    round((self.gate.last_gap or 0) * 1000),
                    size,
                )


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
    if isinstance(error, (httpx.HTTPError, OSError, TransportError)):
        return f"网络或文件操作失败（{type(error).__name__}）；未输出 Cookie 或下载地址"
    return f"操作失败（{type(error).__name__}）"


class Bilibili:
    def __init__(
        self,
        *,
        gate: RequestGate | None = None,
        transport=None,
        image_transport=None,
        broadcast_factory=BroadcastClient,
        cache_root=None,
        fetch_avatar=False,
    ):
        self.gate = gate or RequestGate()
        self.http = ApiHttpClient(self.gate, transport=transport)
        self.sdk = AsyncBpiClient(http_client=self.http)
        self.session: Session | None = None
        self.image_transport = image_transport
        self.favorites = FavoriteLibrary(self.sdk.fav)
        self.inbox = Inbox(self.sdk)
        self.account_content = AccountContent(self.sdk)
        self.creator_content = CreatorContent(self.sdk)
        self.following_collection = FollowingCollection(self.account_content, self.creator_content)
        self.broadcast_factory = broadcast_factory
        self._live_task = None
        self._live_owner = None
        self._live_client = None
        self.cache_root = cache_root
        self.cache = None
        self.cache_notice = ""
        self.fetch_avatar = fetch_avatar

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.stop_inbox_live()
        self.clear_session()
        await self.sdk.aclose()
        await self.http.aclose()

    def clear_session(self):
        if self._live_task and not self._live_task.done():
            self._live_task.cancel()
        self._live_owner = None
        self.http.cookies.clear()
        self.http.user_agent = ""
        self.session = None
        if self.cache is not None:
            self.cache.close()
            self.cache = None
        self.cache_notice = ""
        self.favorites = FavoriteLibrary(self.sdk.fav)
        self.inbox.clear()
        # A login owns its inbox. A late old-account operation cannot repopulate
        # the new account's state, even if it was already awaiting provider I/O.
        self.inbox = Inbox(self.sdk)
        self.account_content = AccountContent(self.sdk)
        self.creator_content = CreatorContent(self.sdk)
        self.following_collection = FollowingCollection(self.account_content, self.creator_content)

    async def forget_account_cache(self, account_id):
        await self.stop_inbox_live()
        self.clear_session()
        if self.cache_root is not None and account_id is not None:
            AccountCache.forget(self.cache_root, account_id)

    async def read_account_content(self, kind, *, more=False, check=False):
        owner, content = self.session, self.account_content
        if owner is None:
            raise DownloadError("请先登录当前账号")
        view = await content.read(kind, owner.mid, more=more, check=check)
        if self.session is not owner or self.account_content is not content:
            raise DownloadError("账号已变化，未发布旧账号的内容")
        return view

    async def read_creator(self, mid, kind, page=1, *, refresh=False, verify=False, on_update=None):
        owner, content = self.session, self.creator_content
        followings = self.account_content.views.get("followings")
        if (
            owner is None
            or followings is None
            or str(mid) not in {row.key for row in followings.items}
        ):
            raise DownloadError("请先在当前账号的关注名单中选择 UP 主")

        def publish(view):
            if self.session is not owner or self.creator_content is not content:
                raise DownloadError("账号已变化，已停止读取旧账号的关注者内容")
            if on_update:
                on_update(owner, view)

        view = await content.read(
            mid,
            kind,
            page,
            refresh=refresh,
            verify=verify,
            on_update=publish,
            discover=self.check_recent_actions,
        )
        if self.session is not owner or self.creator_content is not content:
            raise DownloadError("账号已变化，未发布旧账号的关注者内容")
        return view

    async def check_recent_actions(self, *, force=False):
        owner, content = self.session, self.creator_content
        if owner is None:
            raise DownloadError("请先登录当前账号")

        def check_current():
            if self.session is not owner or self.creator_content is not content:
                raise DownloadError("账号已变化，未发布旧账号的近期操作")

        refreshed = False
        try:
            refreshed = await content.interactions.refresh_recent(
                owner.mid, force=force, check_current=check_current
            )
        except (
            ApiError,
            AuthenticationError,
            HttpStatusError,
            ResponseDecodeError,
            TransportError,
            DownloadError,
            httpx.HTTPError,
        ) as error:
            check_current()
            if self.gate.stopped:
                raise
            # Optional recent surfaces can be private or drift independently.
            # Keep cached facts and current-page verification usable; this source
            # is blocked until an explicit refresh, rather than retried on a timer.
            log.warning("Recent interaction discovery stopped error=%s", type(error).__name__)
        check_current()
        return content.interactions.notice(), refreshed

    async def collect_followings(self, *, restart=False, on_update=None):
        owner, collection = self.session, self.following_collection
        if owner is None:
            raise DownloadError("请先登录当前账号")

        def publish(progress):
            if self.session is not owner or self.following_collection is not collection:
                raise DownloadError("账号已变化，已停止旧账号的关注采集")
            if on_update:
                on_update(owner, progress)

        progress = await collection.run_round(owner.mid, restart=restart, on_update=publish)
        publish(progress)
        return progress

    async def use_cookie(self, cookie: str, *, user_agent: str = "") -> Session:
        await self.stop_inbox_live()
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
            if self.cache_root is not None:
                try:
                    self.cache = AccountCache(self.cache_root, nav.mid)
                    self.favorites = FavoriteLibrary(self.sdk.fav, cache=self.cache)
                    self.inbox = Inbox(self.sdk, cache=self.cache)
                    self.account_content = AccountContent(self.sdk, cache=self.cache)
                    self.creator_content = CreatorContent(self.sdk, cache=self.cache)
                    self.following_collection = FollowingCollection(
                        self.account_content, self.creator_content, cache=self.cache
                    )
                    count = sum(len(view.messages) for view in self.inbox.threads.values())
                    self.cache_notice = (
                        f"已恢复 {len(self.inbox.threads)} 个会话、{count} 条本地消息；"
                        "联网后按游标补增量。"
                    )
                    log.info(
                        "Account cache restored threads=%s messages=%s",
                        len(self.inbox.threads),
                        count,
                    )
                except DownloadError as error:
                    if self.cache is not None:
                        self.cache.close()
                    self.cache = None
                    self.favorites = FavoriteLibrary(self.sdk.fav)
                    self.inbox = Inbox(self.sdk)
                    self.account_content = AccountContent(self.sdk)
                    self.creator_content = CreatorContent(self.sdk)
                    self.following_collection = FollowingCollection(
                        self.account_content, self.creator_content
                    )
                    self.cache_notice = f"{error}；本次仅使用内存，未覆盖原缓存。"
            avatar = b""
            if self.fetch_avatar and nav.face:
                try:
                    avatar = await self.cdn_image(nav.face)
                except (DownloadError, httpx.HTTPError):
                    pass  # A missing avatar must not turn valid authentication into failure.
            self.session = Session(
                nav.mid,
                nav.uname or str(nav.mid),
                normalized_cookie(cookie),
                user_agent,
                avatar,
            )
            return self.session
        except BaseException:
            self.clear_session()
            raise

    def start_inbox_live(self, on_update, on_status):
        """Start once for this exact login, not again after every foreground action."""
        session = self.session
        if session is None or self._live_owner is session:
            return
        self._live_owner = session
        if not session.user_agent or parse_cookie(session.cookie).get("DedeUserID") != str(
            session.mid
        ):
            on_status(
                session, BroadcastState("stopped", "实时收信需要 Chrome 登录及匹配的账号 Cookie")
            )
            return
        self._live_task = asyncio.create_task(
            self._run_inbox_live(session, self.inbox, on_update, on_status)
        )

    async def stop_inbox_live(self):
        task, self._live_task = self._live_task, None
        self._live_owner = None
        self._live_client = None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def check_inbox_live(self, reason):
        if (
            self.session is not None
            and self._live_owner is self.session
            and self._live_client is not None
            and not self.gate.stopped
        ):
            self._live_client.request_check(reason)

    async def _run_inbox_live(self, session, inbox, on_update, on_status):
        pending, talkers, refresh = asyncio.Event(), set(), False
        bootstrapped = False

        def current():
            return self.session is session and self.inbox is inbox and self._live_owner is session

        def status(value):
            nonlocal bootstrapped
            if current():
                on_status(session, value)
                if value.kind == "connected" and not bootstrapped:
                    bootstrapped = True
                    hint(Wakeup(refresh_sessions=True))
                elif value.kind == "connecting":
                    bootstrapped = False

        def hint(value):
            nonlocal refresh
            talkers.update(value.talkers)
            refresh |= value.refresh_sessions
            if len(talkers) > 256:
                raise DownloadError("实时收信待处理会话过多，已暂停；请手动检查")
            if talkers or refresh:
                pending.set()

        def publish(count=0):
            if current():
                on_update(session, inbox.view, inbox.thread, count)

        async def consume():
            nonlocal refresh
            while True:
                await pending.wait()
                # Bounded trailing debounce: fold fetch/list/unread hints for one DM,
                # but continuous traffic cannot postpone acquisition indefinitely.
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    pending.clear()
                    try:
                        await asyncio.wait_for(
                            pending.wait(), min(0.35, deadline - time.monotonic())
                        )
                    except TimeoutError:
                        break
                targets, needs_heads = set(talkers), refresh or bool(talkers)
                talkers.clear()
                refresh = False
                pending.clear()
                known = {
                    row.session.talker_id
                    for row in inbox.view.conversations
                    if row.session.session_type == 1
                }
                heads_read = not known or not targets or not targets.issubset(known)
                if heads_read:
                    await inbox.refresh_heads(session.mid)
                    # On explicit reconnect, reconcile only previously read threads
                    # whose provider frontier advanced while the socket was down.
                    targets.update(
                        row.session.talker_id
                        for row in inbox.view.conversations
                        if row.session.session_type == 1
                        and row.session.talker_id in inbox.high_water
                        and row.session.max_seqno > inbox.high_water[row.session.talker_id]
                    )
                    publish()
                for target in sorted(targets):
                    count = await inbox.sync_talker(target, session.mid)
                    log.info("Broadcast directed sync messages=%s", count)
                    publish(count)
                if needs_heads and not heads_read:
                    await inbox.refresh_heads(session.mid)
                    publish()

        tasks, client = [], None
        try:
            client = self.broadcast_factory(session, self.gate)
            self._live_client = client
            tasks = [asyncio.create_task(client.run(hint, status)), asyncio.create_task(consume())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except asyncio.CancelledError:
            raise
        except Exception as error:
            text = safe_error(error)
            log.warning("Broadcast stopped: %s", text)
            if current():
                on_status(session, BroadcastState("stopped", text))
        finally:
            if self._live_client is client:
                self._live_client = None
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def cached_image(self, value):
        """Local-only lookup; displayed hits are renewed together by Qt."""
        value = canonical_image_url(value)
        if value and self.cache is not None:
            return self.cache.image(value, touch=False)
        return None

    async def cdn_image(self, value: str) -> bytes:
        value = canonical_image_url(value)
        if not value:
            return b""
        cache = self.cache
        if cache is not None:
            body = cache.image(value)
            if body is not None:
                return body
        # No login cookies, redirects or arbitrary hosts. Same request gate as the API.
        async with httpx.AsyncClient(
            transport=self.image_transport, timeout=10, trust_env=False
        ) as images:
            async with self.gate.request():
                started = time.monotonic()
                status, size, outcome = 0, 0, "error"
                try:
                    async with images.stream(
                        "GET",
                        value,
                        follow_redirects=False,
                        headers={
                            "User-Agent": self.http.user_agent or "Mozilla/5.0",
                            "Referer": "https://www.bilibili.com/",
                        },
                    ) as response:
                        status = response.status_code
                        self.gate.check(status)
                        outcome = "rejected"
                        if status != 200:
                            return b""
                        if response.headers.get("content-type", "").split(";")[0] not in {
                            "image/jpeg",
                            "image/png",
                            "image/webp",
                            "image/gif",
                        }:
                            return b""
                        outcome = "error"
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            size += len(chunk)
                            if size > MAX_IMAGE_BYTES:
                                outcome = "too_large"
                                return b""
                            body.extend(chunk)
                        result = bytes(body)
                        if cache is not None:
                            cache.put_image(value, result)
                        outcome = "complete"
                        return result
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                finally:
                    log.info(
                        "HTTP request=%s endpoint=cdn.image method=GET status=%s "
                        "outcome=%s duration_ms=%s gap_ms=%s body_bytes=%s",
                        self.gate.count,
                        status,
                        outcome,
                        round((time.monotonic() - started) * 1000),
                        round((self.gate.last_gap or 0) * 1000),
                        size,
                    )

    def touch_images(self, urls):
        if self.cache is not None:
            self.cache.touch_images(
                {canonical for url in urls if (canonical := canonical_image_url(url))}
            )

    async def send_message(self, talker_id, text):
        if self.session is None:
            raise DownloadError("请先登录当前账号")
        cookie = parse_cookie(self.session.cookie)
        if cookie.get("DedeUserID") != str(self.session.mid) or not cookie.get("bili_jct"):
            raise DownloadError("登录缺少发送所需的身份或验证字段，请重新登录；未发送")
        return await self.inbox.send(talker_id, self.session.mid, text)

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
