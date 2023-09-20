from copy import deepcopy
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout, QWidget, QCheckBox, QPlainTextEdit, QLayout, QLineEdit, QPushButton, QLabel
from multiprocessing import Process, Pipe
from threading import Thread, Lock
from widget.waiting_bar import start_new_bar
from entry_point import process
import sys


def process_(conf):
    import entry_point as ep
    func_name = conf["func_name"]
    params = conf["params"]
    if hasattr(ep, func_name):
        getattr(ep, func_name)(**params)
    else:
        raise ValueError("func '{}' not defined")


class ParamsWidget(QWidget):
    append_text = pyqtSignal(str)
    reset_console1_text = pyqtSignal(str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.cfg = deepcopy(cfg)
        self.debug = False
        self.all_pages = False
        self.widgets = []
        for key in self.cfg["params"]:
            tmp_label = QLabel()
            tmp_label.setText(key)
            tmp_lineedit = QLineEdit()
            tmp_lineedit.setText(self.cfg["params"][key])
            tmp_lineedit.textChanged.connect(
                lambda text, key=key: self.cfg["params"].update({key: text}))
            self.widgets.append([tmp_label, tmp_lineedit])
        self.btn_submit = QPushButton("Submit")
        self.btn_submit.clicked.connect(self.on_button_click)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self.on_abort_click)
        self.btn_stop.hide()


        self.check_dev = QCheckBox()
        self.check_dev.setText("dev")
        self.check_rich = QCheckBox()
        self.check_rich.setText("debug")
        self.check_rich.stateChanged.connect(lambda: self.debug_mode())
        self.check_all_pages = QCheckBox()
        self.check_all_pages.setText("all pages")
        self.check_all_pages.stateChanged.connect(lambda: self.all_pages_mode())

        self.widgets.append(
            [self.check_dev, self.check_rich, self.check_all_pages, self.btn_submit, self.btn_stop])

        self.console = QPlainTextEdit()
        self.console1 = QLineEdit()
        self.console.setReadOnly(True)
        self.console1.setReadOnly(True)
        tmp_vlayout = QVBoxLayout()
        tmp_vlayout.addWidget(self.console)
        tmp_vlayout.addWidget(self.console1)
        tmp_vlayout.setSpacing(0)
        tmp_vlayout.setContentsMargins(0, 0, 0, 0)
        self.widgets.append([tmp_vlayout])

        self.main_layout = QVBoxLayout()

        for items in self.widgets:
            if not isinstance(items, list):
                items = [items]
            if len(items) == 1:
                if isinstance(items[0], QLayout):
                    self.main_layout.addLayout(items[0])
                else:
                    self.main_layout.addWidget(items[0])
            elif len(items) > 1:
                tmp_layout = QHBoxLayout()
                for item in items:
                    tmp_layout.addWidget(item)
                self.main_layout.addLayout(tmp_layout)
        self.setLayout(self.main_layout)
        self.append_text.connect(self.slot_append_text)
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            self.check_dev.hide()
            self.check_rich.hide()

    def append_text4thread(self, text=''):
        self.append_text.emit(text)

    def slot_append_text(self, text=''):
        if text.startswith('[') and text[1].islower():
            text = ']'.join(text.split(']')[1:])
        self.console.appendPlainText(text)

    def on_button_click(self):
        # function call that takes the params as arguments
        self.btn_submit.setEnabled(False)
        self.btn_submit.hide()
        self.parent.dock_operate_page.combobox_switch.setEnabled(False)
        target_cfg = deepcopy(self.cfg)
        target_cfg["params"]["credential"] = self.parent.credential
        target_cfg["params"]["progress"] = None if self.debug else True
        target_cfg["params"]["all_pages"] = True if self.all_pages else False
        main_conn, worker_conn = Pipe()
        if self.check_dev.isChecked():
            # disable multiprocess to debug
            start_new_bar(
                **{"func": process, "params_dict": {"conf": target_cfg, "conn": worker_conn}})
        else:
            p = Process(target=start_new_bar, kwargs={
                "func": process, "params_dict": {"conf": target_cfg, "conn": worker_conn}})
            p.start()

            v = {1: True}
            l = Lock()
            Thread(target=self.waiting_worker, args=[p, v, l,]).start()
            Thread(target=self.updating_worker, args=[
                   v, l, p, main_conn,]).start()
            self.proc2stop = p
        self.btn_stop.setEnabled(True)
        self.btn_stop.show()

    def on_abort_click(self):
        if getattr(self, 'proc2stop', None):
            self.proc2stop.terminate()
        self.append_text4thread(f'>>>> task terminated manually >>>>')
        self.btn_stop.setEnabled(False)

    def waiting_worker(self, worker_proc, val, lock):
        # default_text = self.btn_submit.text()
        # self.btn_submit.setText("Executing")

        worker_proc.join()
        with lock:
            val[1] = False
        # self.btn_submit.setText(default_text)
        self.btn_submit.setEnabled(True)
        self.btn_stop.hide()
        self.btn_submit.show()
        self.parent.dock_operate_page.combobox_switch.setEnabled(True)

    def updating_worker(self, val, lock, proc=None, conn=None):
        from time import sleep
        while True:
            with lock:
                if not val[1]:
                    break
            if conn and not conn.closed and proc.is_alive():
                try:
                    if conn.poll():
                        message = conn.recv()
                        if isinstance(message, str):
                            if message.startswith('0@'):
                                # refreshable statement
                                self.console1.setText(message.split('@')[1])
                            elif message.startswith('1@'):
                                # stage statement
                                self.append_text4thread(message.split('@')[1])
                                # self.console.appendPlainText(message.split('@')[1])
                            elif message.startswith('2@'):
                                # warning statement
                                self.append_text4thread(message.split('@')[1])
                            elif message.startswith('5@'):
                                # image url statement
                                url = [x for x in message.split('@')[1][2:].split('|') if x][0]
                                self.parent.dock_present_page.load_image_from_url(url)
                except Exception as e:
                    print('conn end#{}'.format(str(e)))
                    conn.close()
                    conn = None
            sleep(0.001)

    def debug_mode(self):
        self.debug = self.check_rich.isChecked()

    def all_pages_mode(self):
        self.all_pages = self.check_all_pages.isChecked()
