import qrcode
from time import sleep
from requests import Session as req_session
# thanks for code from bilibili video BV15p4y1X79J


class Login:
    def __init__(self, show_qrcode_method, after_method) -> None:
        self.oauthKey = ""
        self.qrcodeUrl = ""
        self.session = req_session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:62.0) Gecko/20100101 Firefox/62.0"
            }
        )
        self.show_method = show_qrcode_method
        self.after_method = after_method

    def _requests(self, method, url, decode_level=2, retry=10, timeout=15, **kwargs):
        if method in ["get", "post"]:
            for _ in range(retry + 1):
                try:
                    response = getattr(self.session, method)(
                        url, timeout=timeout, **kwargs
                    )
                    return (
                        response.json()
                        if decode_level == 2
                        else response.content
                        if decode_level == 1
                        else response
                    )
                except NotImplementedError:
                    pass
            return None

    def getQRCode(self):
        req = self._requests("get", "https://passport.bilibili.com/qrcode/getLoginUrl")
        if req and req.get("code") == 0:
            self.oauthKey = req["data"]["oauthKey"]
            self.qrcodeUrl = req["data"]["url"]
            return True

    def showQRCode(self, url):
        qrcode_img = qrcode.QRCode()
        qrcode_img.add_data(url)
        qrcode_img = qrcode_img.make_image()
        qrcode_img.save("qrcode.png")
        self.show_method("qrcode.png")

    def login(self):
        if self.getQRCode():
            self.showQRCode(self.qrcodeUrl)
            while True:
                sleep(0.5)
                data = {
                    "oauthKey": self.oauthKey,
                    "gourl": "https://passport.bilibili.com/account/security",
                }
                req = self._requests(
                    "post",
                    "https://passport.bilibili.com/qrcode/getLoginInfo",
                    data=data,
                )
                code = req["data"]
                if code == -4:
                    # wait for scan
                    pass
                elif req["data"] == -2:
                    # regenerate qrcode for timeout
                    self.getQRCode()
                    self.showQRCode(self.qrcodeUrl)
                elif code == -5:
                    # wait for confirm after scan
                    pass
                else:
                    break
            raw_cookies = req["data"]["url"].split("?")[1].split("&")
            cookies = {}
            for cookie in raw_cookies:
                key, val = cookie.split("=")
                if key != "gourl" and key != "Expires":
                    cookies[key] = val
            self.after_method(cookies)
            return cookies


if __name__ == "__main__":
    ins = Login()
    ins.login()