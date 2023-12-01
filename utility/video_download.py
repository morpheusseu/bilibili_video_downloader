import os
import time
import json
import asyncio
import aiohttp
from bilibili_api import video, Credential
from rich.console import Console
from rich.progress import (
    Progress,
    BarColumn,
    MofNCompleteColumn,
    SpinnerColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from pathvalidate import sanitize_filename
from subprocess import DEVNULL, run as subprocess_run

from utility.util import (
    abspath_s,
    retry_task,
    transmit_progress_msg_thread,
    transmit_progress_msg,
    time_format_sec2hhmmss,
    http_range_partition,
)
from threading import Thread
import sys

passport = abspath_s(__file__, "../../configuration/passport.json")
user_config = abspath_s(__file__, "../../configuration/user_configurations.json")
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    # Running as compiled
    passport = abspath_s(sys._MEIPASS, "configuration/passport.json")
    user_config = abspath_s(sys._MEIPASS, "configuration/user_configurations.json")

SESSDATA = ""
BILI_JCT = ""
BUVID3 = ""
ACCOUNT = {}

MAX_TRY_TIME = 3
destination_location = "W:"
# FFMPEG exe filepath, best add into system variable
FFMPEG_PATH = "ffmpeg"


def load_passport():
    global SESSDATA
    global BILI_JCT
    global BUVID3
    global ACCOUNT
    with open(passport) as r_f:
        passport_content = json.load(r_f)
        SESSDATA = passport_content["SESSDATA"]
        BILI_JCT = passport_content["BILI_JCT"]
        BUVID3 = passport_content["BUVID3"]
        ACCOUNT = {"SESSDATA": SESSDATA, "BILI_JCT": BILI_JCT, "BUVID3": BUVID3}


def save_passport(pp):
    res_pp = {}
    for key in ["SESSDATA", "BILI_JCT", "BUVID3"]:
        if key not in pp:
            return
        res_pp[key] = pp[key]
    with open(passport, "w+") as w_f:
        json.dump(res_pp, w_f)
    load_passport()


def load_user_cfg():
    global MAX_TRY_TIME
    global destination_location
    global FFMPEG_PATH
    with open(user_config) as r_f:
        user_cfg_content = json.load(r_f)
        MAX_TRY_TIME = int(user_cfg_content["max_retry_time"])
        destination_location = user_cfg_content["save_location"]
        FFMPEG_PATH = user_cfg_content["ffmpeg_path"]


def save_user_cfg(cfg):
    res_cfg = {}
    for key in ["max_retry_time", "save_location", "ffmpeg_path"]:
        if key not in cfg:
            return
        res_cfg[key] = cfg[key]
    with open(user_config, "w+") as w_f:
        json.dump(res_cfg, w_f)
    load_user_cfg()


load_user_cfg()


def convert_via_ffmpeg(
    output_type,
    input_files,
    output_file=None,
    default_file="video_{}".format(int(time.time()) % 100000000),
    progress=None,
):
    output_file = output_file if output_file else default_file
    output_file = ".".join([output_file, output_type])
    cmd_format_dict = {
        "mp3": '{} -hide_banner -loglevel error -i {} -vn -acodec libmp3lame -y "{}"',
        "wav": '{} -hide_banner -loglevel error -i {} -vn -y "{}"',
        "mp4": '{} -hide_banner -loglevel error -i {} -vcodec copy -acodec copy "{}"',
    }
    abs_output_filepath = abspath_s(destination_location, output_file)
    cmd = cmd_format_dict[output_type].format(
        FFMPEG_PATH,
        " -i ".join(input_files) if isinstance(input_files, list) else input_files,
        abs_output_filepath,
    )
    subprocess_run(cmd, env=dict(os.environ), stdout=DEVNULL, shell=True)
    if progress is None:
        print(abs_output_filepath)


async def get_video_info(video_ins: video.Video):
    infos = {}
    infos["tags"] = await video_ins.get_tags()
    infos["info"] = await video_ins.get_info()
    infos["stat"] = await video_ins.get_stat()
    infos["pages"] = await video_ins.get_pages()
    infos["coins"] = await video_ins.get_pay_coins()
    return infos


all_pages = False
max_progress_index_count = 5


async def video_converter(
    convert_type,
    bv_id,
    credential,
    page_idx=0,
    cid=None,
    progress=None,
    conn=None,
    static_info=None,
):
    global all_pages
    global max_progress_index_count
    type_skip_dict = {
        # (video_skip, audio_skip)
        "audio_only": (True, False),
        "video": (False, False),
        "video_only": (False, True),
    }

    def get_skip_type(cvt_type):
        audio_only = ["wav", "mp3"]
        video = ["mp4"]
        video_only = []
        if cvt_type in audio_only:
            return "audio_only"
        elif cvt_type in video:
            return "video"
        elif cvt_type in video_only:
            return "video_only"
        else:
            print("'.{}' is unsupported".format(cvt_type))
            return

    video_skip, audio_skip = type_skip_dict[get_skip_type(convert_type)]
    video_ins = video.Video(bvid=bv_id, credential=credential)
    video_info = static_info if static_info else await video_ins.get_info()
    v_num = 1 if "videos" not in video_info else int(video_info["videos"])
    v_title = video_info["title"]
    v_upper = video_info["owner"]["name"]
    v_pic = video_info["pic"]
    v_pages = [] if "pages" not in video_info else video_info["pages"]
    _destination_location = destination_location

    async def download_via_cid(
        video_ins=video_ins, progress=progress, page_idx=page_idx, cid=cid
    ):
        # get download url
        url = await retry_task(
            func=video_ins.get_download_url, max_retry_time=5, progress=progress
        )(page_index=page_idx, cid=cid)
        if url is None:
            return

        HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
        video_temp_file = abspath_s(_destination_location, "video_temp.m4s")
        audio_temp_file = abspath_s(_destination_location, "audio_temp.m4s")
        video_convert_index = (
            progress.add_task(
                "[blue]get {}.{} from {}".format(v_title, convert_type, v_upper),
                total=1,
            )
            if progress
            else None
        )
        video_convert_task = (
            [x for x in progress.tasks if x.id == video_convert_index][0]
            if progress
            else None
        )
        if progress and len(progress.tasks) > max_progress_index_count:
            finished_1st_task_id = [
                task.id for task in progress.tasks if task.finished
            ][0]
            progress.remove_task(finished_1st_task_id)
        output_filepath = abspath_s(
            _destination_location, ".".join([sanitize_filename(v_title), convert_type])
        )
        print("Q => {}".format(output_filepath)) if progress is None else None
        if os.path.isfile(output_filepath):
            progress.update(
                video_convert_index,
                description="[yellow][Skip] {}.{}".format(v_title, convert_type),
                advance=1,
            ) if progress else None
            transmit_progress_msg(
                task=video_convert_task,
                conn=conn,
                level=1,
                extra_msg_after=time_format_sec2hhmmss(
                    video_convert_task.finished_time
                    if video_convert_task.finished_time
                    else 0
                ),
            ) if progress else None
            if progress is None:
                print("{} skipped".format(output_filepath))
            return
        transmit_progress_msg(
            task=None, level=5, conn=conn, extra_msg_before=v_pic
        ) if progress else None
        async with aiohttp.ClientSession() as sess:
            # get video stream
            if video_skip:
                video_stream_index = (
                    progress.add_task("[yellow]skip video stream", total=0)
                    if progress
                    else None
                )
            else:
                video_url = url["dash"]["video"][0]["baseUrl"]
                video_stream_index = None
                with open(video_temp_file, "wb"):
                    # create an empty temp file
                    pass
                async with sess.get(video_url, headers=HEADERS) as resp:
                    total_length = resp.headers.get("content-length")
                    video_stream_index = (
                        progress.add_task(
                            "[green]get video stream", total=int(total_length)
                        )
                        if progress
                        else None
                    )
                    cur_task = (
                        [x for x in progress.tasks if x.id == video_stream_index][0]
                        if progress
                        else None
                    )
                    Thread(
                        target=transmit_progress_msg_thread,
                        kwargs={
                            "task": cur_task,
                            "conn": conn,
                            "level": 0,
                            "extra_msg_before": "vp",
                            "slot_sec": 0.01,
                        },
                    ).start() if progress else None
                range_pool = http_range_partition(total_length)
                for part_range in range_pool:
                    # print(f"part{part_range} start")
                    sub_headers = HEADERS.copy()
                    sub_headers["Range"] = f"bytes={part_range[0]}-{part_range[1]}"
                    async with sess.get(video_url, headers=sub_headers) as resp:
                        with open(video_temp_file, "ab") as f:
                            while True:
                                chunk = await resp.content.read(1024)
                                if not chunk:
                                    break
                                progress.update(
                                    video_stream_index, advance=len(chunk)
                                ) if progress else None
                                f.write(chunk)
                progress.update(
                    video_convert_index,
                    total=video_convert_task.total + cur_task.total,
                    completed=video_convert_task.completed + cur_task.completed,
                    finished_time=cur_task.finished_time
                    if not video_convert_task.finished_time
                    else video_convert_task.finished_time + cur_task.finished_time,
                ) if progress else None
            # get audio stream
            if audio_skip:
                audio_stream_index = (
                    progress.add_task("[yellow]skip audio stream", total=0)
                    if progress
                    else None
                )
            else:
                audio_url = url["dash"]["audio"][0]["baseUrl"]
                audio_stream_index = None
                with open(audio_temp_file, "wb"):
                    # create an empty temp file
                    pass
                async with sess.get(audio_url, headers=HEADERS) as resp:
                    total_length = resp.headers.get("content-length")
                    audio_stream_index = (
                        progress.add_task(
                            "[blue]get audio stream", total=int(total_length)
                        )
                        if progress
                        else None
                    )
                    cur_task = (
                        [x for x in progress.tasks if x.id == audio_stream_index][0]
                        if progress
                        else None
                    )
                    Thread(
                        target=transmit_progress_msg_thread,
                        kwargs={
                            "task": cur_task,
                            "conn": conn,
                            "level": 0,
                            "extra_msg_before": "ap",
                            "slot_sec": 0.01,
                        },
                    ).start() if progress else None
                range_pool = http_range_partition(total_length)
                for part_range in range_pool:
                    # print(f"part{part_range} start")
                    sub_headers = HEADERS.copy()
                    sub_headers["Range"] = f"bytes={part_range[0]}-{part_range[1]}"
                    async with sess.get(audio_url, headers=sub_headers) as resp:
                        with open(audio_temp_file, "ab") as f:
                            while True:
                                chunk = await resp.content.read(1024)
                                if not chunk:
                                    break
                                progress.update(
                                    audio_stream_index, advance=len(chunk)
                                ) if progress else None
                                f.write(chunk)
                progress.update(
                    video_convert_index,
                    total=video_convert_task.total + cur_task.total,
                    completed=video_convert_task.completed + cur_task.completed,
                    finished_time=cur_task.finished_time
                    if not video_convert_task.finished_time
                    else video_convert_task.finished_time + cur_task.finished_time,
                ) if progress else None
            progress.update(
                video_convert_index,
                description="[blue]converting {}.{} from {}".format(
                    v_title, convert_type, v_upper
                ),
            ) if progress else None
            # mix streams
            convert_via_ffmpeg(
                output_type=convert_type,
                input_files=[audio_temp_file]
                if video_skip
                else [video_temp_file]
                if audio_skip
                else [audio_temp_file, video_temp_file],
                # output_filepath.split('.')[0],
                output_file=".".join(output_filepath.split(".")[:-1]),
                progress=progress,
            )
            # delete temp file
            if not video_skip:
                os.remove(video_temp_file)
            if not audio_skip:
                os.remove(audio_temp_file)
            progress.remove_task(video_stream_index) if progress else None
            progress.remove_task(audio_stream_index) if progress else None
            progress.update(
                video_convert_index,
                description="[green][Done] get {}.{} from {}".format(
                    v_title, convert_type, v_upper
                ),
                advance=1,
            ) if progress else None
            transmit_progress_msg(
                task=video_convert_task,
                conn=conn,
                level=1,
                extra_msg_after=time_format_sec2hhmmss(
                    video_convert_task.finished_time
                    if video_convert_task.finished_time
                    else 0
                ),
            ) if progress else None

    if all_pages and v_num > 1:
        _destination_location = abspath_s(
            destination_location, sanitize_filename(v_title)
        )
        os.mkdir(_destination_location) if not os.path.isdir(
            _destination_location
        ) else None
        for page in v_pages:
            cid = page["cid"]
            v_title = page["part"]
            try:
                await download_via_cid(cid=cid)
            except Exception as e:
                print("error:{}.dvc".format(str(e)))
            time.sleep(0.1)
    else:
        await download_via_cid()


async def video_converter_batch(video_infos, convert_type, credential, progress):
    for v_info in video_infos:
        v_bvid = v_info["bvid"]
        v_title = v_info["title"]
        v_upper = v_info["upper"]
        await video_converter(
            convert_type=convert_type,
            bv_id=v_bvid,
            credential=credential,
            page_idx=0,
            progress=progress,
            static_info={"title": v_title, "owner": v_info["owner"]},
        )
        import time
        import random

        time.sleep(random.randint(1, 4))
        yield
