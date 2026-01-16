import time
import threading
import socket
from .abstract_tunnel import AbstractTunnel
from common.kcp import Kcp

class KcpTunnelImpl(AbstractTunnel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kcp = Kcp(12345)
        self.kcp.output = self._udp_output
        self.kcp.setmtu(800) # 关键：强制小于 1000
        self.kcp.nodelay(1, 10, 2, 1)
        self.lock = threading.Lock()
        self.running = True

    def establish(self):
        # KCP 是无状态的，打洞成功即认为建立成功
        threading.Thread(target=self._recv_loop, daemon=True).start()
        # 立即通知 Manager 成功
        self.on_established(self.peer_name)

    def _udp_output(self, buf):
        try: self.sock.sendto(buf, self.addr)
        except: pass

    def _recv_loop(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65536)
                with self.lock:
                    self.kcp.input(data)
            except: break

    def update(self):
        with self.lock:
            self.kcp.update(int(time.time() * 1000))
            while True:
                data = self.kcp.recv()
                if not data: break
                if len(data) >= 4:
                    self.on_data_received(data[:4], data[4:])

    def send(self, uid: bytes, data: bytes) -> bool:
        with self.lock:
            return self.kcp.send(uid + data) >= 0

    def close(self):
        self.running = False
        try: self.sock.close()
        except: pass
        self.on_closed(self.peer_name)