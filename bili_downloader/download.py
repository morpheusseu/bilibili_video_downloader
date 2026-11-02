"""Streaming download and local FFmpeg conversion, shared by GUI and CLI."""

import asyncio
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urljoin, urlparse

import httpx
import imageio_ffmpeg

from .api import Bilibili
from .sources import DownloadError, Source, VideoPart


@dataclass(frozen=True)
class Progress:
    phase: str
    message: str
    completed: int = 0
    total: int | None = None
    # Current output file, not the whole (possibly still paginating) collection.
    # Equal stream stages occupy 95%; only atomic publication reaches 100%.
    percent: int | None = None


@dataclass(frozen=True)
class Result:
    path: Path
    skipped: bool = False


def ffmpeg_executable(configured: str = "") -> str:
    if configured.strip():
        executable = shutil.which(configured.strip())
        if not executable:
            raise DownloadError("指定的 FFmpeg 不存在或不可执行")
        return executable
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def media_url(primary: str, backups: list[str]) -> str:
    for value in [primary, *backups]:
        url = urlparse(value)
        host = url.hostname or ""
        if (
            url.scheme == "https"
            and not url.username
            and not url.password
            and url.netloc == host
            and any(
                host == suffix or host.endswith("." + suffix)
                for suffix in (
                    "bilivideo.com",
                    "bilivideo.cn",
                    "bilivideo.net",
                    "acgvideo.com",
                    "akamaized.net",
                    "v.smtcdns.com",  # Observed HTTPS target from the selected provider CDN.
                )
            )
        ):
            return value
    raise DownloadError("播放接口没有返回受信任的 HTTPS 媒体地址")


async def transfer(
    http,
    gate,
    url: str,
    target: Path,
    report: Callable[[Progress], None],
    *,
    user_agent="Mozilla/5.0",
    label="媒体流",
):
    # A separate cookie-free client prevents account credentials reaching a media CDN.
    for hop in range(4):
        async with gate.request():
            async with http.stream(
                "GET",
                url,
                headers={
                    "User-Agent": user_agent,
                    "Referer": "https://www.bilibili.com/",
                    "Accept-Encoding": "identity",
                },
                follow_redirects=False,
            ) as response:
                gate.check(response.status_code)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise DownloadError("CDN 重定向缺少目标地址")
                    url = media_url(urljoin(url, location), [])
                    report(Progress("redirect", f"{label} · CDN 安全转发 {hop + 1}/4"))
                    continue
                if response.status_code != 200:
                    raise DownloadError(f"媒体下载返回 HTTP {response.status_code}，未自动重试")
                length = response.headers.get("content-length")
                total = int(length) if length and length.isdecimal() else None
                completed = 0
                with target.open("xb") as output:
                    async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                        output.write(chunk)
                        completed += len(chunk)
                        report(Progress("bytes", f"下载{label}", completed, total))
                if completed == 0 or (total is not None and completed != total):
                    raise DownloadError("下载长度不完整，没有生成最终文件；可以手动重新运行")
                return
    raise DownloadError("CDN 重定向次数超过上限，已停止")


async def convert(executable: str, inputs: list[Path], target: Path, *, combined: bool):
    args = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-n"]
    if combined and len(inputs) > 1:
        listing = inputs[0].parent / "segments.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in inputs), encoding="utf-8")
        args += ["-f", "concat", "-safe", "1", "-i", str(listing)]
    else:
        for path in inputs:
            args += ["-i", str(path)]
    if target.suffix == ".mp4":
        args += ["-map", "0:v:0"]
        if combined:
            args += ["-map", "0:a:0?"]
        elif len(inputs) == 2:
            args += ["-map", "1:a:0"]
        args += ["-c", "copy", "-movflags", "+faststart"]
    else:
        args += [
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "libmp3lame" if target.suffix == ".mp3" else "pcm_s16le",
        ]
    args.append(str(target))
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.communicate(), 5)
            except TimeoutError:
                process.kill()
                await process.communicate()
        raise
    if process.returncode or not target.is_file() or target.stat().st_size == 0:
        # FFmpeg only receives local files; do not expose its arbitrary stderr in the UI.
        raise DownloadError(f"FFmpeg 转换失败（退出码 {process.returncode}）；未发布不完整文件")


async def download_part(
    api: Bilibili,
    part: VideoPart,
    output: Path,
    format: str,
    quality: int,
    executable: str,
    report: Callable[[Progress], None],
    *,
    media_transport=None,
) -> Result:
    target = output / f"{part.stem}.{format}"
    if target.exists():
        if target.is_symlink() or not target.is_file() or target.stat().st_size == 0:
            raise DownloadError("目标位置存在空文件、目录或链接，请先更换保存位置/清理后重试")
        report(Progress("skipped", f"已存在，跳过：{target.name}"))
        return Result(target, skipped=True)
    report(Progress("metadata", f"获取播放信息：{part.title} / P{part.number}"))
    data = await api.streams(part, quality)
    combined = data.dash is None
    transfers = []
    if data.dash:
        if format == "mp4":
            streams = [v for v in data.dash.video if v.id <= quality]
            compatible = [v for v in streams if v.codecs.startswith("avc1")]
            stream = max(compatible or streams, key=lambda x: (x.id, x.bandwidth), default=None)
            if stream is None:
                raise DownloadError("此内容没有不高于所选画质的可用视频流")
            transfers.append(("视频流（画面）", media_url(stream.base_url, stream.backup_url)))
        audio = max(data.dash.audio, key=lambda x: x.bandwidth, default=None)
        if audio:
            transfers.append(("音频流（声音）", media_url(audio.base_url, audio.backup_url)))
        elif format != "mp4":
            raise DownloadError("此内容没有可用音频流")
    elif data.durl:
        segments = sorted(data.durl, key=lambda d: d.order)
        transfers = [
            (f"音视频片段 {index + 1}/{len(segments)}（同一视频）", media_url(d.url, d.backup_url))
            for index, d in enumerate(segments)
        ]
    else:
        raise DownloadError("播放接口没有返回支持的 DASH 或分段流")
    output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".bili-download-", dir=output) as temporary:
        work = Path(temporary)
        inputs = []
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=20), trust_env=False, transport=media_transport
        ) as http:
            for index, (label, url) in enumerate(transfers):
                path = work / f"{index}.m4s"
                report(
                    Progress(
                        "download",
                        f"下载{label}：{part.title} / P{part.number}",
                        percent=int(95 * index / len(transfers)),
                    )
                )

                def report_transfer(event, *, index=index):
                    percent = None
                    if event.phase == "bytes" and event.total:
                        percent = int(
                            95 * (index + min(1, event.completed / event.total)) / len(transfers)
                        )
                    report(replace(event, percent=percent))

                await transfer(
                    http,
                    api.gate,
                    url,
                    path,
                    report_transfer,
                    user_agent=api.http.user_agent or "Mozilla/5.0",
                    label=label,
                )
                inputs.append(path)
        staged = work / f"result.{format}"
        action = (
            "合并视频流与音频流，生成 1 个 MP4"
            if not combined and format == "mp4" and len(transfers) == 2
            else f"转换并生成 1 个 {format.upper()} 文件"
        )
        report(Progress("convert", f"{action}：{part.title}", percent=95))
        await convert(executable, inputs, staged, combined=combined)
        try:
            # Atomic publication without overwriting a file created by another job.
            if os.name == "nt":
                os.rename(staged, target)  # Windows rename does not replace existing targets.
            else:
                os.link(staged, target)
        except FileExistsError:
            report(Progress("skipped", f"文件已由其他任务生成，跳过：{target.name}"))
            return Result(target, skipped=True)
    report(Progress("done", f"完成：{target.name}", percent=100))
    return Result(target)


async def download_source(
    api: Bilibili,
    source: Source,
    output: Path,
    *,
    format="mp4",
    quality=64,
    all_parts=False,
    page=1,
    ffmpeg="",
    report: Callable[[Progress], None] = lambda event: None,
    media_transport=None,
) -> list[Result]:
    if format not in {"mp4", "mp3", "wav"}:
        raise DownloadError("输出格式只能是 MP4、MP3 或 WAV")
    executable = ffmpeg_executable(ffmpeg)
    results = []
    async for part in api.parts(source, all_parts=all_parts, page=page):
        results.append(
            await download_part(
                api,
                part,
                output,
                format,
                quality,
                executable,
                report,
                media_transport=media_transport,
            )
        )
    report(
        Progress(
            "summary",
            f"任务结束：生成 {sum(not r.skipped for r in results)} 个文件，"
            f"跳过 {sum(r.skipped for r in results)} 个；没有匹配内容时不会生成文件",
        )
    )
    return results
