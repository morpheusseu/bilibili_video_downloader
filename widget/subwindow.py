from copy import deepcopy
from PyQt5 import QtWidgets
from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout, QWidget, QCheckBox
from multiprocessing import Process, Pipe
from threading import Thread, Lock


def process_(conf):
    import entry_point as ep
    func_name = conf["func_name"]
    params = conf["params"]
    if hasattr(ep, func_name):
        getattr(ep, func_name)(**params)
    else:
        raise ValueError("func '{}' not defined")


class ParamsWidget(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.cfg = deepcopy(cfg)
        self.debug = False
        self.all_pages = False
        self.widgets = []
        for key in self.cfg["params"]:
            tmp_label = QtWidgets.QLabel()
            tmp_label.setText(key)
            tmp_lineedit = QtWidgets.QLineEdit()
            tmp_lineedit.setText(self.cfg["params"][key])
            tmp_lineedit.textChanged.connect(
                lambda text, key=key: self.cfg["params"].update({key: text}))
            self.widgets.append([tmp_label, tmp_lineedit])
        self.btn_submit = QtWidgets.QPushButton()
        self.btn_submit.setText("Submit")
        self.btn_submit.clicked.connect(self.on_button_click)

        self.check_rich = QCheckBox()
        self.check_rich.setText("debug")
        self.check_rich.stateChanged.connect(lambda: self.debug_mode())
        self.check_all_pages = QCheckBox()
        self.check_all_pages.setText("all pages")
        self.check_all_pages.stateChanged.connect(
            lambda: self.all_pages_mode())

        self.widgets.append(
            [self.check_rich, self.check_all_pages, self.btn_submit])

        self.main_layout = QVBoxLayout()

        for items in self.widgets:
            if not isinstance(items, list):
                items = [items]
            if len(items) == 1:
                self.main_layout.addWidget(items[0])
            elif len(items) > 1:
                tmp_layout = QHBoxLayout()
                for item in items:
                    tmp_layout.addWidget(item)
                self.main_layout.addLayout(tmp_layout)
        self.setLayout(self.main_layout)

    def on_button_click(self):
        # function call that takes the params as arguments
        target_cfg = deepcopy(self.cfg)
        target_cfg["params"]["credential"] = self.parent.credential
        target_cfg["params"]["progress"] = None if self.debug else True
        target_cfg["params"]["all_pages"] = True if self.all_pages else False
        from waiting_bar import start_new_bar
        from entry_point import process
        main_conn, worker_conn = Pipe()
        p = Process(target=start_new_bar, kwargs={
                    "func": process, "params_dict": {"conf": target_cfg, "conn": worker_conn}})
        p.start()
        v = {1: True}
        l = Lock()
        Thread(target=self.waiting_worker, args=[p, v, l,]).start()
        Thread(target=self.updating_worker, args=[v, l, p, main_conn,]).start()

    def waiting_worker(self, worker_proc, val, lock):
        self.btn_submit.setEnabled(False)
        worker_proc.join()
        with lock:
            val[1] = False

    def updating_worker(self, val, lock, proc=None, conn=None):
        import time
        default_text = self.btn_submit.text()
        i = 1
        dot = ['', '.', '..', '...']
        message = "Executing"
        while i < 4:
            if conn and proc.is_alive():
                try:
                    if conn.poll():
                        message = conn.recv()
                except Exception as e:
                    print(e)
                    conn.close()
                    conn = None
            self.btn_submit.setText("{} {}".format(message, dot[i]))
            with lock:
                if not val[1]:
                    break
            time.sleep(0.001)
            # i = i + 1 if i < 3 else 1
        self.btn_submit.setText(default_text)
        self.btn_submit.setEnabled(True)

    def debug_mode(self):
        self.debug = self.check_rich.isChecked()

    def all_pages_mode(self):
        self.all_pages = self.check_all_pages.isChecked()
