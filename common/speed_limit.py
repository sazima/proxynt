import time
import threading
from typing import Tuple


class SpeedLimiter:
    """
    令牌桶限速器（发送端限速）

    使用方式：
        limiter = SpeedLimiter(max_speed_mb=1.0)  # 1 MB/s

        # 同步使用
        wait_time = limiter.acquire(len(data))
        if wait_time > 0:
            time.sleep(wait_time)
        send(data)

        # 异步使用
        wait_time = limiter.acquire(len(data))
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        await send(data)
    """

    def __init__(self, max_speed_mb=0):
        """
        :param max_speed_mb: 最大速度，单位 MB/s，0 表示不限速
        """
        self.max_speed = max_speed_mb * 1024 * 1024  # 转换为字节/秒
        self.tokens = self.max_speed  # 初始令牌数（允许突发）
        self.last_time = time.time()
        self.lock = threading.Lock()

    def acquire(self, data_len):
        """
        获取发送许可

        :param data_len: 要发送的数据长度（字节）
        :return: 需要等待的时间（秒），0 表示不需要等待
        """
        if self.max_speed <= 0:
            return 0

        with self.lock:
            now = time.time()
            elapsed = now - self.last_time
            self.last_time = now

            # 补充令牌（按时间流逝补充，最多补充到 max_speed）
            self.tokens = min(self.max_speed, self.tokens + elapsed * self.max_speed)

            # 消耗令牌
            self.tokens -= data_len

            if self.tokens >= 0:
                return 0  # 有足够令牌，不需要等待

            # 令牌不足，计算需要等待的时间
            wait_time = -self.tokens / self.max_speed
            return wait_time

    def set_max_speed(self, max_speed_mb):
        """
        动态调整限速

        :param max_speed_mb: 新的最大速度，单位 MB/s
        """
        with self.lock:
            self.max_speed = max_speed_mb * 1024 * 1024
            # 重置令牌，避免突然加速
            self.tokens = min(self.tokens, self.max_speed)

    # 保留旧接口的兼容性（但不推荐使用）
    def add(self, data_len):
        """兼容旧接口，等同于 acquire 但不返回等待时间"""
        self.acquire(data_len)

    def is_exceed(self) -> Tuple[bool, float]:
        """兼容旧接口，返回 (是否超速, 剩余量)"""
        if self.max_speed <= 0:
            return False, 0
        with self.lock:
            now = time.time()
            elapsed = now - self.last_time
            # 不更新 last_time，只是检查
            current_tokens = min(self.max_speed, self.tokens + elapsed * self.max_speed)
            if current_tokens >= 0:
                return False, current_tokens
            return True, -current_tokens
