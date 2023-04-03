import asyncio
import argparse
from bilibili_api import Credential
from bilibili_api.user import User
from favorite_list import get_favorite_lists_from_upper_by_uid

DEFAULT_UID2QUERY = 1778026586


async def verify_via_get_user_info(credential, uid):
    user = User(credential=credential, uid=uid)
    user_info = await user.get_user_info()
    print(user_info)


async def main(credential, uid):
    await verify_via_get_user_info(credential=credential, uid=uid)
    list_info = await get_favorite_lists_from_upper_by_uid(credential=credential, uid=uid)
    print(list_info)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-buvid3', type=str, help='user buvid3')
    parser.add_argument('-bili_jct', type=str, help='user bili_jct')
    parser.add_argument('-sessdata', type=str, help='user sessdata')
    args = parser.parse_args()

    credential = Credential(sessdata=args.sessdata,
                            bili_jct=args.bili_jct, buvid3=args.buvid3)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(main(credential=credential, uid=DEFAULT_UID2QUERY))
