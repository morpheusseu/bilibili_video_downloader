from PyQt5.QtWidgets import QProgressBar, QDialog, QMainWindow, QApplication
from PyQt5.QtCore import QThread, pyqtSignal


class TaskThread(QThread):
    taskFinished = pyqtSignal(int)

    def __init__(self, func, params_dict=None, parent=None):
        super().__init__(parent)
        self.task = func
        self.params = params_dict

    def run(self):
        try:
            self.task(**self.params) if self.params else self.task()
            self.taskFinished.emit(0)
        except Exception as e:
            print("error : {}".format(str(e)))
            self.taskFinished.emit(1)


class TaskBar(QDialog):
    def __init__(self, func, params_dict=None, parent=None, is_show=False):
        super().__init__(parent)
        self.progress = QProgressBar(self)
        self.progress.setGeometry(0, 0, 300, 25)
        self.show() if is_show else None

        self.worker = TaskThread(func=func, params_dict=params_dict)
        self.worker.taskFinished.connect(self.done)
        self.worker.start()
        self.worker.wait()
        exit(0)


def start_new_bar(func, params_dict):
    import sys
    app = QApplication(sys.argv)
    TaskBar(func=func, params_dict=params_dict)
    sys.exit(app.exec_())
