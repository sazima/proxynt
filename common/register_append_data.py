from common.speed_limit import SpeedLimiter
from constant.system_constant import SystemConstant


MSS = 1460
class ResisterAppendData:
    """注册socket事件的附带信息"""
    def __init__(self, callable_: callable, speed_limiter: SpeedLimiter = None):
        self.callable_ = callable_
        self.speed_limiter = speed_limiter  # type: SpeedLimiter
        self.read_size = SystemConstant.CHUNK_SIZE


