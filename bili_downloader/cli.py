"""The CLI and GUI share the same provider session and download engine."""

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from .api import Bilibili, safe_error
from .auth import load_session, save_session
from .browser_login import chrome_login
from .download import download_source
from .sources import DownloadError, parse_source


async def run(args):
    async with Bilibili() as api:
        if args.command == "login":
            print("将在独立 Chrome 临时会话中打开 Bilibili；请手动扫码或完成官方登录。")
            session = await chrome_login(api, print)
            if args.remember:
                await asyncio.to_thread(save_session, session)
            print(f"登录成功：{session.name}（{session.mid}）")
            return
        if args.use_saved_login:
            saved = await asyncio.to_thread(load_session)
            if saved is None:
                raise DownloadError("没有保存的登录；先运行 bili-download login --remember")
            session = await api.use_cookie(saved.cookie, user_agent=saved.user_agent)
            if session.mid != saved.mid:
                raise DownloadError("保存的登录账号不一致，已停止")
        source = parse_source(args.source, args.mode, args.pattern)
        if args.command == "inspect":
            count = 0
            async for part in api.parts(source, all_parts=args.all_parts, page=args.page):
                print(json.dumps(asdict(part), ensure_ascii=False))
                count += 1
                if count >= args.limit:
                    print(f"预览达到 {args.limit} 条上限；不声明已遍历全部内容。")
                    break
            return

        def report(event):
            if event.phase != "bytes":
                print(event.message, flush=True)

        await download_source(
            api,
            source,
            args.output.expanduser().resolve(),
            format=args.format,
            quality=args.quality,
            all_parts=args.all_parts,
            page=args.page,
            ffmpeg=args.ffmpeg,
            report=report,
        )


def main():
    parser = argparse.ArgumentParser(description="bpi-py Bilibili 视频/音频下载器")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser(
        "login", help="在真实 Chrome 中登录；--remember 保存到本机未加密文件"
    )
    login.add_argument("--remember", action="store_true")
    for name in ("inspect", "download"):
        command = commands.add_parser(name)
        command.add_argument("source", help="BV/AV/ep ID、完整链接、收藏夹 ID 或 UID")
        command.add_argument("--mode", choices=["auto", "favorite", "user"], default="auto")
        command.add_argument("--pattern", default="", help="UID 模式下的收藏夹名称正则")
        command.add_argument("--page", type=int, default=1, help="分 P，从 1 开始")
        command.add_argument("--all-parts", action="store_true")
        command.add_argument("--use-saved-login", action="store_true")
        if name == "inspect":
            command.add_argument("--limit", type=int, default=20)
        else:
            command.add_argument(
                "--output", type=Path, default=Path.home() / "Downloads" / "bilibili"
            )
            command.add_argument("--format", choices=["mp4", "mp3", "wav"], default="mp4")
            command.add_argument("--quality", type=int, choices=[16, 32, 64, 80], default=64)
            command.add_argument("--ffmpeg", default="")
    args = parser.parse_args()
    if args.command == "inspect" and args.limit < 1:
        parser.error("--limit 必须大于 0")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as error:
        parser.exit(1, safe_error(error) + "\n")
