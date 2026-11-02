"""User-driven login in real Chrome, isolated from existing browser profiles."""

import asyncio
from collections.abc import Callable

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from .api import Bilibili, safe_error
from .session import COOKIE_NAMES, Session
from .sources import DownloadError


async def chrome_login(
    api: Bilibili,
    status: Callable[[str], None],
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> Session:
    """No credentials are typed, challenges solved or existing profiles inspected."""
    if api.gate.stopped:
        raise DownloadError("当前实例已因风控/限流停止；请先在官方客户端检查，不会另开登录绕过")
    api.clear_session()
    browser = None
    stage = "初始化浏览器控制"
    try:
        async with async_playwright() as playwright:
            try:
                # Installed branded Chrome, NOT a downloaded Chromium or spoofed Android UA.
                # Playwright owns and removes its temporary browser profile on close.
                stage = "启动 Chrome"
                status("1/4 · 正在启动独立 Chrome 登录窗口")
                browser = await playwright.chromium.launch(channel="chrome", headless=False)
                stage = "准备独立会话"
                context = await browser.new_context(no_viewport=True)
                page = await context.new_page()
                # Read from the untouched blank page. Once sign-in sets cookies,
                # the original page may navigate or close; handoff must not evaluate it.
                stage = "读取浏览器信息"
                agent = await page.evaluate("navigator.userAgent")
                stage = "打开官方登录页"
                await page.goto(
                    "https://passport.bilibili.com/login",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                stage = "等待扫码确认"
                status("2/4 · 请在 Chrome 的 Bilibili 官方页面扫码并确认；完成后会自动交接")
                deadline = asyncio.get_running_loop().time() + 300
                while asyncio.get_running_loop().time() < deadline:
                    if check_cancelled is not None:
                        check_cancelled()
                    stage = "读取登录状态"
                    if not browser.is_connected():
                        raise DownloadError("登录窗口已关闭；未改变已有 Chrome 登录")
                    # CDP reads local browser state; this is not a Bilibili HTTP poll.
                    cookies = await context.cookies(
                        ["https://www.bilibili.com/", "https://api.bilibili.com/"]
                    )
                    pairs = {c["name"]: c["value"] for c in cookies if c["name"] in COOKIE_NAMES}
                    if pairs.get("SESSDATA") and pairs.get("DedeUserID"):
                        stage = "验证账号身份"
                        status("3/4 · 已检测到登录，正在验证账号身份；不会自动重试")
                        session = await api.use_cookie(
                            "; ".join(f"{k}={v}" for k, v in pairs.items()), user_agent=agent
                        )
                        if str(session.mid) != pairs["DedeUserID"]:
                            api.clear_session()
                            raise DownloadError("浏览器与 API 返回的账号不一致，已停止")
                        if check_cancelled is not None:
                            check_cancelled()
                        status("4/4 · 账号身份已验证，正在关闭本次临时登录窗口")
                        return session
                    if not context.pages:
                        raise DownloadError("登录窗口已关闭，尚未完成扫码确认；可重新开始")
                    await asyncio.sleep(1)
                stage = "等待扫码确认"
                raise DownloadError("等待扫码确认超时（5 分钟）；可以重新开始")
            finally:
                if browser and browser.is_connected():
                    try:
                        await browser.close()
                    except BrowserError:
                        # Cleanup must not erase the primary failure or a verified account.
                        status(
                            "清理提示［关闭登录窗口］：未确认 Chrome 已关闭，请手动关闭本次临时窗口"
                        )
    except BaseException as error:
        api.clear_session()
        if not isinstance(error, Exception):
            raise
        if isinstance(error, BrowserError):
            # Inspect only known categories; never expose raw Playwright diagnostics,
            # which can contain URLs, browser arguments or credential-bearing content.
            if isinstance(error, BrowserTimeout):
                reason = "浏览器操作超时；请检查网络或官方页面后重新开始，不会自动重试"
            elif "Execution context was destroyed" in str(error):
                reason = "页面跳转中断了浏览器操作；可重新开始，不代表账号验证失败"
            elif "closed" in str(error).lower():
                reason = "临时浏览器或控制连接已关闭；可重新开始，未改变其他 Chrome 登录"
            elif stage == "启动 Chrome":
                reason = "无法启动已安装的 Google Chrome；请检查安装，不会自动安装或替换浏览器"
            else:
                reason = "Chrome 控制通信失败；可重新开始，未读取已有浏览器配置"
        else:
            reason = safe_error(error)
        raise DownloadError(f"登录未完成［{stage}］：{reason}") from None
