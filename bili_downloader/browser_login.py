"""User-driven login in real Chrome, isolated from existing browser profiles."""

import asyncio
from collections.abc import Callable

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from .api import Bilibili
from .auth import COOKIE_NAMES, Session
from .sources import DownloadError


async def chrome_login(api: Bilibili, status: Callable[[str], None]) -> Session:
    """No credentials are typed, challenges solved or existing profiles inspected."""
    if api.gate.stopped:
        raise DownloadError("当前实例已因风控/限流停止；请先在官方客户端检查，不会另开登录绕过")
    api.clear_session()
    browser = None
    try:
        async with async_playwright() as playwright:
            try:
                # Installed branded Chrome, NOT a downloaded Chromium or spoofed Android UA.
                # Playwright owns and removes its temporary browser profile on close.
                browser = await playwright.chromium.launch(channel="chrome", headless=False)
                context = await browser.new_context(no_viewport=True)
                page = await context.new_page()
                await page.goto(
                    "https://passport.bilibili.com/login",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                status("Chrome 已打开：请在 Bilibili 官方页面扫码/登录；完成后会自动回到下载器")
                deadline = asyncio.get_running_loop().time() + 300
                while asyncio.get_running_loop().time() < deadline:
                    if not browser.is_connected() or page.is_closed():
                        raise DownloadError("登录窗口已关闭；未改变已有 Chrome 登录")
                    # CDP reads local browser state; this is not a Bilibili HTTP poll.
                    cookies = await context.cookies(
                        ["https://www.bilibili.com/", "https://api.bilibili.com/"]
                    )
                    pairs = {c["name"]: c["value"] for c in cookies if c["name"] in COOKIE_NAMES}
                    if pairs.get("SESSDATA") and pairs.get("DedeUserID"):
                        agent = await page.evaluate("navigator.userAgent")
                        session = await api.use_cookie(
                            "; ".join(f"{k}={v}" for k, v in pairs.items()), user_agent=agent
                        )
                        if str(session.mid) != pairs["DedeUserID"]:
                            api.clear_session()
                            raise DownloadError("浏览器与 API 返回的账号不一致，已停止")
                        return session
                    await asyncio.sleep(1)
                raise DownloadError("等待 Chrome 登录超时；可以重新开始")
            finally:
                if browser and browser.is_connected():
                    await browser.close()
    except BrowserError:
        api.clear_session()
        raise DownloadError(
            "无法完成 Chrome 登录。请确认已安装 Google Chrome，或检查官方登录页；"
            "不自动安装/替换系统浏览器"
        ) from None
    except BaseException:
        api.clear_session()
        raise
