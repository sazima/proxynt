from abc import ABC, abstractmethod
from typing import Callable

class AbstractTunnel(ABC):
    def __init__(self, peer_name: str, sock, addr, on_data_received: Callable, on_established: Callable, on_closed: Callable):
        self.peer_name = peer_name
        self.sock = sock
        self.addr = addr
        self.on_data_received = on_data_received # 应用层收到数据回调
        self.on_established = on_established     # 握手成功回调
        self.on_closed = on_closed               # 隧道关闭回调

    @abstractmethod
    def establish(self):
        """开始协议层面的握手/初始化"""
        pass

    @abstractmethod
    def send(self, uid: bytes, data: bytes) -> bool:
        """发送业务数据"""
        pass

    @abstractmethod
    def update(self):
        """驱动状态机（如 KCP 需要）"""
        pass

    @abstractmethod
    def close(self):
        """物理关闭"""
        pass