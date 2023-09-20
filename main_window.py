import os
import sys
import json
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, QDockWidget, QWidget, QComboBox, \
    QPushButton, QLabel, QLineEdit, QMessageBox
from PyQt5.QtGui import QPalette, QColor, QIcon, QPixmap
from PyQt5.QtCore import Qt, pyqtSignal
from asyncio import new_event_loop, log
from requests import get as req_get
from threading import Thread, Lock
from multiprocessing import freeze_support
from time import sleep
from bilibili_api.user import get_self_info
from bilibili_api import Credential
from utility.util import abspath_s
from utility.qrcode_login import Login
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    Bin_Dir = abspath_s(sys._MEIPASS, "bin")
    Image_Location = abspath_s(sys._MEIPASS, "image.png")
    Github_Img = abspath_s(sys._MEIPASS, "github.png")
else:
    Bin_Dir = abspath_s(__file__, "..", "bin")
    Image_Location = "image.png"
    Github_Img = "github.png"
if os.path.isdir(Bin_Dir):
    os.environ['PATH'] = f"{Bin_Dir};{os.environ.get('PATH', '')}"


class PresentPage(QDockWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.online = False
        self.main_widget = QWidget()
        self.setFeatures(QDockWidget.DockWidgetMovable)
        self.image_label = QLabel()
        # self.image_label.setFixedWidth(250)
        layout = QVBoxLayout()
        layout.addWidget(self.image_label, alignment=Qt.AlignCenter)
        self.main_widget.setLayout(layout)
        self.setWidget(self.main_widget)
        self.setWindowTitle("Present Page")
        self.pwd_is_dirty = False
        self.lock = Lock()
        Thread(target=self.get_user, args=[lambda: self.parent.credential, ]).start()

    def dirty(self):
        with self.lock:
            val = self.pwd_is_dirty
            self.pwd_is_dirty = False
        return val

    def set_dirty(self):
        # once passport updated
        with self.lock:
            self.pwd_is_dirty = True

    @staticmethod
    def _get_self_user_info(credential, loop):
        try:
            user_info = loop.run_until_complete(get_self_info(credential=credential))
        except Exception as e:
            print(f'_get_self_user_info: {e}')
            return None
        return user_info

    def get_user(self, credential_getter, retry_time=3):
        loop = new_event_loop()
        retry = 0
        while True:
            try:
                try:
                    self.isHidden()
                except RuntimeError:
                    return
                user_info = self._get_self_user_info(credential=credential_getter(), loop=loop)
                if user_info is None:
                    raise NotImplementedError
                if self.online:
                    # heart beats
                    retry = retry_time
                    continue
                self.setWindowTitle(f"welcome! {user_info['name']} (lv.{user_info['level']})")
                self.load_image_from_url(user_info['face'])
                self.online = True
                sleep(1)
            except NotImplementedError:
                self.online = False
                if retry > 0:
                    retry -= 1
                    continue
                try:
                    self.setWindowTitle("please login via qrcode")
                except NotImplementedError as ni_e:
                    # Windows Deleted might be
                    print(f"Widgets Deleted: {ni_e}")
                    exit(0)
                ins = Login(show_qrcode_method=self.load_image, after_method=self.process_cookies,
                            interrupt_judge=self.dirty, stderr_method=self.parent.error_propagate4thread)
                t = Thread(target=ins.login, args=[])
                t.start()
                t.join()
                break

    def process_cookies(self, cookies):
        if cookies:
            print(f'get cookies {cookies}')
            setting_page = self.parent.dock_setting_page
            setting_page: SettingPage
            for key in cookies:
                if key.lower() == 'sessdata':
                    getattr(setting_page, "lineedit_SESSDATA").setText(cookies[key])
                elif key.lower() == 'bili_jct':
                    getattr(setting_page, "lineedit_BILI_JCT").setText(cookies[key])
                elif key.lower() == 'buvid3':
                    getattr(setting_page, "lineedit_BUVID3").setText(cookies[key])
            setting_page.on_button_click()
        Thread(target=self.get_user, args=[lambda: self.parent.credential, ]).start()

    def load_image_from_url(self, url):
        # Save the image data to a file
        with open(Image_Location, 'wb') as f:
            f.write(req_get(url, timeout=10).content)
        # Load the image into a QPixmap
        pixmap = QPixmap(Image_Location)
        pixmap = pixmap.scaled(250, 250, Qt.KeepAspectRatio)
        self.setFixedSize(pixmap.width() + 50, pixmap.height() + 50)
        self.image_label.setPixmap(pixmap)

    def load_image(self, filepath):
        pixmap = QPixmap()
        pixmap.load(filepath)
        self.setFixedSize(pixmap.width() + 50, pixmap.height() + 50)
        self.image_label.setPixmap(pixmap)


class SettingPage(QDockWidget):
    from utility.video_download import user_config, passport

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.init_values()
        self.main_widget = QWidget()
        self.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.setWindowTitle("Bilibili-api settings")
        with open(self.user_config) as r_f:
            self.user_cfg_content = json.load(r_f)
        with open(self.passport) as r_f:
            self.passport_content = json.load(r_f)
        dynamic_widgets = []
        for idx, group in enumerate([self.passport_content, self.user_cfg_content]):
            for key in group:
                setattr(self, "label_{}".format(key), QLabel())
                getattr(self, "label_{}".format(key)).setText(key)
                setattr(self, "lineedit_{}".format(key), QLineEdit())
                getattr(self, "lineedit_{}".format(key)).setText(group[key])
                getattr(self, "lineedit_{}".format(key)).setEchoMode(QLineEdit.PasswordEchoOnEdit) if idx == 0 else None
                if key in self.passport_content:
                    getattr(self, "lineedit_{}".format(key)).textChanged.connect(
                        lambda text, key=key: self.passport_content.update({key: text}))
                if key in self.user_cfg_content:
                    getattr(self, "lineedit_{}".format(key)).textChanged.connect(
                        lambda text, key=key: self.user_cfg_content.update({key: text}))
                dynamic_widgets.append(
                    [
                        getattr(self, "label_{}".format(key)),
                        getattr(self, "lineedit_{}".format(key))
                    ]
                )
        self.btn_submit = QPushButton()
        self.btn_submit.setText("Save Configuration")
        self.btn_submit.clicked.connect(self.on_button_click)

        self.main_layout = QVBoxLayout()

        for items in [*dynamic_widgets, [self.btn_submit]]:
            if len(items) == 1:
                self.main_layout.addWidget(items[0])
            elif len(items) > 1:
                tmp_layout = QHBoxLayout()
                for item in items:
                    tmp_layout.addWidget(item)
                self.main_layout.addLayout(tmp_layout)
        self.main_widget.setLayout(self.main_layout)
        self.setWidget(self.main_widget)

    def init_values(self):
        self.credential = None

    def get_credential(self):
        # self.on_button_click()
        return self.credential

    def on_button_click(self):
        self.credential = Credential(
            sessdata=getattr(self, "lineedit_SESSDATA").text(),
            bili_jct=getattr(self, "lineedit_BILI_JCT").text(),
            buvid3=getattr(self, "lineedit_BUVID3").text()
        )
        from utility.video_download import save_user_cfg, save_passport
        save_user_cfg(self.user_cfg_content)
        save_passport(self.passport_content)
        self.parent.dock_present_page.set_dirty()


class OperatePage(QDockWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.setFeatures(QDockWidget.NoDockWidgetFeatures)
        self.init_value()
        self.setWindowTitle("Bilibili-api operator")
        self.main_widget = QWidget()
        self.main_layout = QVBoxLayout()

        tmp_layout = self.get_switch_box()

        self.main_layout.addLayout(tmp_layout)
        self.main_widget.setLayout(self.main_layout)
        self.setWidget(self.main_widget)
        self.create_new_page()

    def init_value(self):
        self.configurations = None
        with open(abspath_s(__file__, '../configuration/configurations.json'), 'r') as r_f:
            self.configurations = json.load(r_f)

    def get_switch_box(self):
        tmp_layout = QHBoxLayout()
        _size = 40
        github_btn = QLabel()
        github_btn.setText(f"<a href='https://github.com/morpheusseu/bilibili_video_downloader'><img src='{Github_Img}' width='{_size}' height='{_size}'></a>")
        github_btn.setOpenExternalLinks(True)
        github_btn.setToolTip("Click to open the GitHub repository")
        github_btn.setFixedSize(_size, _size)
        if not hasattr(self, "combobox_switch"):
            self.combobox_switch = QComboBox(self)
            for conf in self.configurations:
                self.combobox_switch.addItem(conf["name"], conf["func_id"])
            self.combobox_switch.currentIndexChanged.connect(
                self.create_new_page)
        for widget in [self.combobox_switch, github_btn]:
            tmp_layout.addWidget(widget)
        return tmp_layout

    def refresh_widget(self, widget):
        self.main_widget = QWidget()
        self.main_layout = QVBoxLayout()
        self.main_layout.addWidget(widget)
        self.main_layout.addLayout(self.get_switch_box())
        self.main_widget.setLayout(self.main_layout)
        self.setWidget(self.main_widget)

    def create_new_page(self):
        from widget.subwindow import ParamsWidget
        for conf in self.configurations:
            if conf["func_id"] == self.combobox_switch.currentData():
                new_widget = ParamsWidget(
                    parent=self.parent, cfg=conf)
                self.refresh_widget(new_widget)
                self.setWindowTitle(conf["name"])
                break


class MainWindow(QMainWindow):
    error_propagate = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setGeometry(200, 200, 800, 400)
        self.setWindowTitle("Video/Audio downloader via bilibili-api")
        self.setWindowIcon(QIcon(abspath_s(__file__, "../icon.png")))
        self.dock_setting_page = SettingPage(parent=self)
        self.dock_operate_page = OperatePage(parent=self)
        self.dock_present_page = PresentPage(parent=self)
        self.change_style(self)
        self.change_style(self.dock_setting_page)
        self.change_style(self.dock_operate_page)
        self.addDockWidget(1, self.dock_operate_page)
        self.addDockWidget(2, self.dock_present_page)
        self.addDockWidget(2, self.dock_setting_page)
        self.dock_setting_page.on_button_click()
        self.err_msgbox = QMessageBox(parent=self)
        self.err_msgbox.setWindowTitle('Error occurred!')
        self.err_msgbox.hide()
        self.error_propagate.connect(self.slot_error_propagate)

    @property
    def credential(self):
        return self.dock_setting_page.get_credential()

    def change_style(self, widget):
        palette_a = QPalette()
        palette_a.setColor(QPalette.WindowText, QColor(0, 0, 0))
        palette_a.setColor(QPalette.Window, QColor(100, 100, 100))
        palette_a.setColor(QPalette.Base, QColor(25, 25, 25))
        palette_a.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
        palette_a.setColor(QPalette.ToolTipBase, QColor(255, 255, 255))
        palette_a.setColor(QPalette.ToolTipText, QColor(0, 0, 255))
        palette_a.setColor(QPalette.Text, QColor(255, 0, 255))
        palette_a.setColor(QPalette.Button, QColor(53, 53, 53))
        palette_a.setColor(QPalette.ButtonText, QColor(0, 0, 0))
        palette_a.setColor(QPalette.BrightText, QColor(255, 0, 0))
        palette_a.setColor(QPalette.Link, QColor(42, 130, 218))
        palette_a.setColor(QPalette.Highlight, QColor(42, 130, 218))
        palette_a.setColor(QPalette.HighlightedText, QColor(35, 35, 35))
        widget.setPalette(palette_a)

    def error_propagate4thread(self, msg):
        self.error_propagate.emit(msg)
    
    def slot_error_propagate(self, msg):
        self.err_msgbox.setText(msg)
        self.err_msgbox.exec()
        exit(-1)
        


def main():
    app = QApplication(sys.argv)
    win = MainWindow()  # this line cause infinitely reopen, the origin is multiprocess, freeze_support needed
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    freeze_support()
    from logging import DEBUG
    log.logger.setLevel(DEBUG)
    main()
