import time

class SpeedLimiter(object):
    def __init__(self, max_speed=0):
        self.max_speed = max_speed * 1024
        self.last_time = time.time()
        self.sum_len = 0

    def update_limit(self, max_speed):
        self.max_speed = max_speed * 1024

    def add(self, data_len):
        if self.max_speed > 0:
            cut_t = time.time()
            self.sum_len -= (cut_t - self.last_time) * self.max_speed
            if self.sum_len < 0:
                self.sum_len = 0
            self.last_time = cut_t
            self.sum_len += data_len

    def free_size(self):
        if self.max_speed > 0:
            cut_t = time.time()
            self.sum_len -= (cut_t - self.last_time) * self.max_speed
            if self.sum_len < 0:
                self.sum_len = 0
            self.last_time = cut_t
            return self.sum_len - self.max_speed  # >=
        return 0
