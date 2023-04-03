import os
import time
import asyncio


def abspath_s(*arg):
    return os.path.abspath(os.path.join(*arg))


def retry_task(func, max_retry_time=3, msg="dealing", progress=None):
    async def wrapper(*args, **kwargs):
        get_url_index = progress.add_task(
            '[blue] {}'.format(msg), total=1) if progress else None
        for i in range(1, max_retry_time+2):
            try:
                val = await func(*args, **kwargs)
                progress.remove_task(get_url_index) if progress else None
                return val
            except Exception:
                progress.update(get_url_index, description='[yellow] {} (retry:{})'.format(
                    msg, i)) if progress else None
                time.sleep(1)
        progress.update(
            get_url_index, description='[red][Failed]{} ({} times retried)'.format(msg, max_retry_time)) if progress else None
        return None
    return wrapper


'''
from subprocess import PIPE, Popen
from utility.util import abspath_s
script_path = abspath_s(
    __file__, '..', 'utility', 'verify_credential.py')
process = Popen("python {} -buvid3 '{}' -bili_jct '{}' -sessdata '{}'".format(script_path, self.lineedit_buvid3.text(),
                self.lineedit_bili_jct.text(), self.lineedit_sess_data.text()), stdout=PIPE, stderr=PIPE)
stdout, stderr = process.communicate()
print("out:{}.\nerr:{}.\n...".format(stdout.decode(), stderr))'''
