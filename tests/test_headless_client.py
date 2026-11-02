"""The independently installable provider client requires no desktop runtime."""

import subprocess
import sys

import httpx
from bilibili_client.api import Bilibili, RequestGate


def test_core_imports_without_desktop_or_host_modules():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
class RejectDesktop:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PyQt5', 'imageio_ffmpeg', 'bili_downloader', 'boxy_desktop'}:
            raise ImportError('Desktop dependency: ' + fullname)
sys.meta_path.insert(0, RejectDesktop())
from bilibili_client.api import Bilibili
from bilibili_client.browser_login import chrome_login
from bilibili_client.broadcast import BroadcastClient
""",
        ],
        check=True,
        capture_output=True,
    )


async def test_headless_identity_verification_never_fetches_avatar_or_cover():
    paths = []

    def provider(request):
        paths.append(request.url.path)
        assert request.url.path == "/x/web-interface/nav"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "isLogin": True,
                    "mid": 42,
                    "uname": "Synthetic user",
                    "face": "https://i0.hdslb.com/face.png",
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/wbi/a.png",
                        "sub_url": "https://i0.hdslb.com/wbi/b.png",
                    },
                },
            },
        )

    async with Bilibili(gate=RequestGate(0, 0), transport=httpx.MockTransport(provider)) as api:
        session = await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert session.mid == 42
        assert session.avatar == b""
    assert paths == ["/x/web-interface/nav"]
