import sys
from asyncio import new_event_loop
from rich.progress import (
    Progress,
    BarColumn,
    MofNCompleteColumn,
    SpinnerColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)


def process(conf, conn):
    ep = sys.modules[__name__]
    func_name = conf["func_name"]
    params = conf["params"]
    params["conn"] = conn
    import utility.video_download as vd

    vd.all_pages = True if params["all_pages"] is True else False
    del params["all_pages"]

    if hasattr(ep, func_name):
        getattr(ep, func_name)(**params)
    else:
        raise ValueError(f"func {func_name} not defined")
    conn.close() if conn else None


def download_videos_from_given_upper_favorite_list(
    credential, uid, convert_type="wav", list_name_re="", progress=None, conn=None
):
    uid = int(uid)
    from utility.favorite_list import get_videos_from_favorite_list_by_cfg

    favorite_lists_config = [
        {"uid": uid, "list_name_re": list_name_re, "convert_type": convert_type}
    ]
    progress = (
        Progress(
            SpinnerColumn(),
            "{task.description}",
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        )
        if progress
        else None
    )
    progress.start() if progress else None
    # entry
    new_event_loop().run_until_complete(
        get_videos_from_favorite_list_by_cfg(
            credential=credential, config_list=favorite_lists_config, progress=progress
        )
    )
    progress.stop() if progress else None


def download_videos_from_given_favorite_list_id(
    credential, fid, convert_type="wav", progress=None, conn=None
):
    from utility.favorite_list import videos_converter_from_given_favorite_list_id

    progress = (
        Progress(
            SpinnerColumn(),
            "{task.description}",
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        )
        if progress
        else None
    )
    progress.start() if progress else None
    # entry
    new_event_loop().run_until_complete(
        videos_converter_from_given_favorite_list_id(
            credential=credential,
            convert_type=convert_type,
            favorite_list_id=fid,
            progress=progress,
            conn=conn,
        )
    )
    progress.stop() if progress else None


def download_video_from_given_bvid(
    convert_type,
    bv_id,
    credential,
    page_idx=0,
    cid=None,
    progress=None,
    conn=None,
    static_info=None,
):
    page_idx = int(page_idx)
    from utility.video_download import video_converter

    progress = (
        Progress(
            SpinnerColumn(),
            "{task.description}",
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        )
        if progress
        else None
    )
    progress.start() if progress else None
    # entry
    new_event_loop().run_until_complete(
        video_converter(
            credential=credential,
            convert_type=convert_type,
            bv_id=bv_id,
            page_idx=page_idx,
            cid=cid,
            progress=progress,
            conn=conn,
            static_info=static_info,
        )
    )
    progress.stop() if progress else None


def download_videos_from_given_link_adjustment(
    web_link, convert_type, credential, progress=None, conn=None
):
    from utility.bangumi import get_bvid_from_episode_info

    progress = (
        Progress(
            SpinnerColumn(),
            "{task.description}",
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        )
        if progress
        else None
    )
    parts = web_link.split("/")
    video_flag = False
    bangumi_flag = False
    for item in parts:
        if item.startswith("http"):
            pass
        if item.startswith("favlist?fid="):
            fid = item.split("=")[1].split("&")[0]
            # get favlist id
            download_videos_from_given_favorite_list_id(
                credential=credential,
                fid=fid,
                convert_type=convert_type,
                progress=progress,
                conn=conn,
            )
            break
        if video_flag:
            if item.startswith("BV"):
                bvid = item
                download_video_from_given_bvid(
                    convert_type=convert_type,
                    bv_id=bvid,
                    credential=credential,
                    progress=progress,
                    conn=conn,
                )
                break
        if bangumi_flag:
            if item.startswith("ep"):
                # get BV num via epid
                epid = int(item.split("?")[0][2:])
                bvid = new_event_loop().run_until_complete(
                    get_bvid_from_episode_info(epid, credential)
                )
                download_video_from_given_bvid(
                    convert_type=convert_type,
                    bv_id=bvid,
                    credential=credential,
                    progress=progress,
                    conn=conn,
                )
                break

        if item == "bangumi":
            bangumi_flag = True
        if item == "video":
            video_flag = True
    progress.stop() if progress else None


def hello(a=0, b=1):
    print("entry point {}|{}".format(a, b))


if __name__ == "__main__":
    a = {"a": 2, "b": 3}
    if hasattr(sys.modules[__name__], "hello"):
        getattr(sys.modules[__name__], "hello")(**a)
