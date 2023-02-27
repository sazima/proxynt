import time
from typing import Tuple


class SpeedLimiter(object):
    def __init__(self, max_speed=0):
        """
        :param max_speed:  单位MB
        """
        self.max_speed = max_speed * 1024 * 1024  # 单位字节
        self.last_time = time.time()
        self.sum_len = 0


    def add(self, data_len):
        if self.max_speed > 0:
            cut_t = time.time()
            self.sum_len -= (cut_t - self.last_time) * self.max_speed
            if self.sum_len < 0:
                self.sum_len = 0
            self.last_time = cut_t
            self.sum_len += data_len

    def is_exceed(self) -> Tuple[bool, float]:
        if self.max_speed > 0:
            cut_t = time.time()
            self.sum_len -= (cut_t - self.last_time) * self.max_speed
            if self.sum_len < 0:
                self.sum_len = 0
            self.last_time = cut_t
            remain = self.sum_len - self.max_speed
            return remain >= 0, remain
        return False, 1
