import copy
import time

from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils


class ClearNonceTask:
    async def run(self):
        nonce_to_time = ContextUtils.get_nonce_to_time()
        copy_cookie_dict = copy.deepcopy(nonce_to_time)
        now = time.time()
        for n, t in copy_cookie_dict.items():
            if abs(now - t) > SystemConstant.MAX_TIME_DIFFERENCE:
                if n in nonce_to_time:
                    nonce_to_time.pop(n)