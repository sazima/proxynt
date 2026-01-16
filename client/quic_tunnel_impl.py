import asyncio
from .abstract_tunnel import AbstractTunnel

class QuicTunnelImpl(AbstractTunnel):
    def establish(self):
        # 这里封装之前 N4TunnelManager 里的 _start_quic_connection 逻辑
        # 调用 self.on_established 或 self.on_closed
        pass

    def send(self, uid: bytes, data: bytes) -> bool:
        # 之前的 aioquic send 逻辑
        pass

    def update(self): pass

    def close(self):
        # 之前的 close 逻辑
        pass