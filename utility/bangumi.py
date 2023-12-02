try:
    from utility.video_download import ACCOUNT
except ImportError:
    from video_download import ACCOUNT
from bilibili_api import bangumi, Credential
import asyncio


async def get_bvid_from_episode_info(epid: int, credential: Credential = None):
    """
    获取番剧单集信息

    Args:
        epid       (int)                 : episode_id
        credential (Credential, optional): 凭据. Defaults to None
    """

    credential = credential if credential is not None else Credential()
    session = bangumi.get_session()

    async with session.get(
        f"https://www.bilibili.com/bangumi/play/ep{epid}",
        cookies=credential.get_cookies(),
        headers={"User-Agent": "Mozilla/5.0"},
    ) as resp:
        if resp.status != 200:
            raise bangumi.ResponseException(resp.status)

        content = await resp.text()
        seq = content.find('"bvid"')
        return (
            content[seq : seq + 100]
            .split(",")[0]
            .split(":")[1]
            .replace("'", "")
            .replace('"', "")
        )


if __name__ == "__main__":
    credential = Credential(
        sessdata=ACCOUNT["SESSDATA"],
        bili_jct=ACCOUNT["BILI_JCT"],
        buvid3=ACCOUNT["BUVID3"],
    )
    loop = asyncio.new_event_loop()
    content = loop.run_until_complete(
        get_bvid_from_episode_info(
            epid=746940,
            credential=credential,
        )
    )
    print(content)
