import copy
import time
from typing import List

from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils


class CheckCookieTask:
    async def run(self):
        cookie_dict = ContextUtils.get_cookie_to_time()
        copy_cookie_dict = copy.deepcopy(ContextUtils.get_cookie_to_time())
        now = time.time()
        for c, t in copy_cookie_dict.items():
            if (now - t) > SystemConstant.COOKIE_EXPIRE_SECONDS:
                if c in cookie_dict:
                    cookie_dict.pop(c)
