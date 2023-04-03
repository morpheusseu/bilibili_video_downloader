import asyncio
from rich.progress import Progress, BarColumn, MofNCompleteColumn, SpinnerColumn, TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn


def download_videos_from_given_upper_favorite_list(credential, uid, convert_type="wav", list_name_re="", progress=None):
    uid = int(uid)
    from utility.favorite_list import get_videos_from_favorite_list_by_cfg
    favorite_lists_config = [
        {
            "uid": uid,
            "list_name_re": list_name_re,
            "convert_type": convert_type
        }
    ]
    progress = Progress(
        SpinnerColumn(), '{task.description}', BarColumn(), MofNCompleteColumn(
        ), TaskProgressColumn(), TimeElapsedColumn(), TimeRemainingColumn()
    ) if progress else None
    progress.start() if progress else None
    # entry
    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        get_videos_from_favorite_list_by_cfg(credential=credential, config_list=favorite_lists_config, progress=progress))
    progress.stop() if progress else None


def download_videos_from_given_favorite_list_id(credential, fid, convert_type="wav", progress=None):
    from utility.favorite_list import videos_converter_from_given_favorite_list_id
    progress = Progress(
        SpinnerColumn(), '{task.description}', BarColumn(), MofNCompleteColumn(
        ), TaskProgressColumn(), TimeElapsedColumn(), TimeRemainingColumn()
    ) if progress else None
    progress.start() if progress else None
    # entry
    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        videos_converter_from_given_favorite_list_id(credential=credential, convert_type=convert_type, favorite_list_id=fid, progress=progress))
    progress.stop() if progress else None


def download_video_from_given_bvid(convert_type, bv_id, credential, page_idx=0, cid=None, progress=None, static_info=None):
    page_idx = int(page_idx)
    from utility.video_download import video_converter
    progress = Progress(
        SpinnerColumn(), '{task.description}', BarColumn(), MofNCompleteColumn(
        ), TaskProgressColumn(), TimeElapsedColumn(), TimeRemainingColumn()
    ) if progress else None
    progress.start() if progress else None
    # entry
    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        video_converter(credential=credential, convert_type=convert_type, bv_id=bv_id,
                        page_idx=page_idx, cid=cid, progress=progress, static_info=static_info)
    )
    progress.stop() if progress else None


def hello(a=0, b=1):
    print("entry point {}|{}".format(a, b))


if __name__ == "__main__":
    import sys
    a = {'a': 2, 'b': 3}
    if hasattr(sys.modules[__name__], "hello"):
        getattr(sys.modules[__name__], "hello")(**a)
