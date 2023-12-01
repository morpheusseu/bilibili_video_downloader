import os
import time


def abspath_s(*arg):
    return os.path.abspath(os.path.join(*arg))


def make_dirctory_recursive(filepath):
    if not os.path.isdir(filepath):
        parent = os.path.dirname(filepath)
        if parent == filepath:
            return True
        if make_dirctory_recursive(parent) is False:
            return False
        try:
            os.makedirs(filepath)
            return True
        except OSError as ose:
            print(f"{ose}")
            return False


def http_range_partition(
    total_length, max_block_size_byte=134217728, ignore_factor=0.5
):
    # 134217728 = 1024 * 1024 * 128, 268435456 = 1024 * 1024 * 256, 536870912 = 1024 * 1024 * 512
    i_f = 1 + ignore_factor
    total_length = int(total_length)
    if total_length <= max_block_size_byte * i_f:
        return [[0, total_length - 1]]
    res = []
    cur_pos = 0
    while total_length - cur_pos >= max_block_size_byte * i_f:
        res.append([cur_pos, cur_pos + max_block_size_byte - 1])
        cur_pos += max_block_size_byte
    res.append([cur_pos, total_length - 1])
    return res


def retry_task(func, max_retry_time=3, msg="dealing", progress=None):
    async def wrapper(*args, **kwargs):
        get_url_index = (
            progress.add_task("[blue] {}".format(msg), total=1) if progress else None
        )
        for i in range(1, max_retry_time + 2):
            try:
                val = await func(*args, **kwargs)
                progress.remove_task(get_url_index) if progress else None
                return val
            except Exception:
                progress.update(
                    get_url_index, description="[yellow] {} (retry:{})".format(msg, i)
                ) if progress else None
                time.sleep(1)
        progress.update(
            get_url_index,
            description="[red][Failed]{} ({} times retried)".format(
                msg, max_retry_time
            ),
        ) if progress else None
        return None

    return wrapper


def transmit_progress_msg_thread(
    task,
    conn,
    level,
    extra_msg_before="",
    extra_msg_after="",
    slot_sec=0.02,
    mk_complete=True,
):
    from time import sleep

    while not task.finished:
        transmit_progress_msg(task, conn, level, extra_msg_before, extra_msg_after)
        sleep(slot_sec) if slot_sec >= 0.02 else sleep(0.02)
    if mk_complete:
        msg = "{}@{}:{}|{}|{}".format(
            level,
            task.description,
            extra_msg_before,
            "{}/{}".format(task.total, task.total),
            extra_msg_after,
        )
        conn.send(msg)


def transmit_progress_msg(task, conn, level, extra_msg_before="", extra_msg_after=""):
    # level : 0->refreshable str;1->stage str;2->warning str;5->image url str
    if task is None:
        msg = "{}@{}:{}|{}|{}".format(
            level, "|", extra_msg_before, "|", extra_msg_after
        )
    else:
        msg = "{}@{}:{}|{}|{}".format(
            level,
            task.description,
            extra_msg_before,
            "{}/{}".format(task.completed, task.total),
            extra_msg_after,
        )
    conn.send(msg)


def time_format_sec2hhmmss(sec):
    from time import strftime, gmtime

    return strftime("%H:%M:%S", gmtime(int(sec)))


"""
from subprocess import PIPE, Popen
from utility.util import abspath_s
script_path = abspath_s(
    __file__, '..', 'utility', 'verify_credential.py')
process = Popen("python {} -buvid3 '{}' -bili_jct '{}' -sessdata '{}'".format(script_path, self.lineedit_buvid3.text(),
                self.lineedit_bili_jct.text(), self.lineedit_sess_data.text()), stdout=PIPE, stderr=PIPE)
stdout, stderr = process.communicate()
print("out:{}.\nerr:{}.\n...".format(stdout.decode(), stderr))"""

if __name__ == "__main__":
    num = int(268435456 * 25.4)
    print(num, http_range_partition(num))
    pass
