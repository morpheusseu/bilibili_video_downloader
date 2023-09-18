from utility.video_download import ACCOUNT, video_converter
from bilibili_api import favorite_list, Credential
import asyncio
import re
from rich.progress import Progress, BarColumn, MofNCompleteColumn, SpinnerColumn, TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn
from utility.util import transmit_progress_msg

UPPER_UID = 398421039
ITEM_PER_PAGE = 20


async def get_favorite_lists_from_upper_by_uid(uid, credential, re_query=None, pruned=True):
    favorite_lists_info = await favorite_list.get_video_favorite_list(uid=uid, credential=credential)
    if favorite_lists_info is None:
        return []
    count = favorite_lists_info["count"]
    favorite_lists_list = favorite_lists_info["list"]
    favorite_lists_list = [
        item for item in favorite_lists_list if re_query is None or re.search(re_query, item["title"])]
    favorite_lists_list_pruned = [
        {"id": item["id"], "title":item["title"], "count":item["media_count"]} for item in favorite_lists_list]
    return favorite_lists_list_pruned if pruned else favorite_lists_list


async def get_video_infos_from_favorite_list_by_id(media_id, credential, pruned=True):
    favorite_list_content = await favorite_list.get_video_favorite_list_content(media_id=media_id, credential=credential, page=1)
    list_count = favorite_list_content["info"]["media_count"]
    list_title = favorite_list_content["info"]["title"]
    video_infos_list = favorite_list_content["medias"]
    page_count = list_count//ITEM_PER_PAGE + \
        1 if list_count % ITEM_PER_PAGE else list_count//ITEM_PER_PAGE
    if page_count > 1:
        for page_id in range(2, page_count+1):
            favorite_list_content = await favorite_list.get_video_favorite_list_content(media_id=media_id, credential=credential, page=page_id)
            video_infos_list += favorite_list_content["medias"]
    video_infos_list_pruned = [
        {"id": item["id"], "bvid":item["bvid"], "title":item["title"], "page":item["page"], "duration":item["duration"], "upper":item["upper"]["name"], "owner":{"name": item["upper"]["name"]}} for item in video_infos_list]
    return video_infos_list_pruned if pruned else video_infos_list


async def videos_converter_from_given_favorite_list(favorite_lists, credential, convert_type='wav', progress=None, conn=None):
    favorite_lists = favorite_lists if isinstance(
        favorite_lists, list) else [favorite_lists]
    list_convert_index = progress.add_task(
        '[blue]get videos from favorite lists', total=sum([item["count"] for item in favorite_lists])) if progress else None
    for f_list in favorite_lists:
        list_id = f_list["id"]
        list_title = f_list["title"]
        list_count = f_list["count"]
        progress.update(
            list_convert_index, description='[green]get videos from "{}"'.format(list_title)) if progress else None
        video_infos = await get_video_infos_from_favorite_list_by_id(media_id=list_id, credential=credential)
        for v_info in video_infos:
            v_bvid = v_info["bvid"]
            v_title = v_info["title"]
            v_upper = v_info["upper"]
            static_info = {"title": v_title, "owner": v_info["owner"]}
            await video_converter(convert_type=convert_type, bv_id=v_bvid, credential=credential, page_idx=0, progress=progress, static_info=None if progress else static_info, conn=conn)
            from time import sleep
            from random import randint
            sleep(randint(1, 4))
            progress.update(list_convert_index,
                            advance=1) if progress else None


async def videos_converter_from_given_favorite_list_id(favorite_list_id, credential, convert_type='wav', progress=None, conn=None):
    from time import sleep
    from random import randint
    video_infos = await get_video_infos_from_favorite_list_by_id(media_id=favorite_list_id, credential=credential)
    list_convert_index = progress.add_task(
        '[green]get videos from favorite list-{}'.format(favorite_list_id), total=len(video_infos)) if progress else None
    list_convert_task = [task for task in progress.tasks if task.id ==
                         list_convert_index][0] if progress else None
    transmit_progress_msg(task=list_convert_task,
                          conn=conn, level=1, extra_msg_before="[Begin]") if progress else None
    for v_info in video_infos:
        v_bvid = v_info["bvid"]
        v_title = v_info["title"]
        v_upper = v_info["upper"]
        static_info = {"title": v_title, "owner": v_info["owner"]}
        await video_converter(convert_type=convert_type, bv_id=v_bvid, credential=credential, page_idx=0, progress=progress, static_info=None if progress else static_info, conn=conn)
        sleep(randint(1, 10) * 0.1)
        progress.update(list_convert_index,
                        advance=1) if progress else None
    transmit_progress_msg(task=list_convert_task,
                          conn=conn, level=1, extra_msg_before="[Finished]") if progress else None


async def get_videos_from_favorite_list_by_cfg(credential, config_list, progress=None, conn=None):
    """
        config_list = [{"uid":uid, "lise_name_re":lise_name_re, "convert_type":convert_type}, {...}, ...]
    """
    for cfg in config_list:
        uid = cfg["uid"]
        list_name_re = cfg["list_name_re"]
        convert_type = cfg["convert_type"]
        target_favorite_lists = await get_favorite_lists_from_upper_by_uid(uid=uid, credential=credential, re_query=list_name_re)
        await videos_converter_from_given_favorite_list(favorite_lists=target_favorite_lists, credential=credential, convert_type=convert_type, progress=progress, conn=conn)


if __name__ == '__main__':
    favorite_lists_config = [
        {
            "uid": UPPER_UID,
            "list_name_re": "piano.*",
            "convert_type": "wav"
        }
    ]
    credential = Credential(
        sessdata=ACCOUNT["SESSDATA"], bili_jct=ACCOUNT["BILI_JCT"], buvid3=ACCOUNT["BUVID3"])
    progress = Progress(
        SpinnerColumn(), '{task.description}', BarColumn(), MofNCompleteColumn(
        ), TaskProgressColumn(), TimeElapsedColumn(), TimeRemainingColumn()
    )
    progress.start()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(
        get_videos_from_favorite_list_by_cfg(credential=credential, config_list=favorite_lists_config, progress=progress))
    progress.stop()
