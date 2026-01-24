import logging
import socket
import select
import time
from typing import Optional, Tuple, List

from common.logger_factory import LoggerFactory
from common.n4_protocol import N4Packet, N4Error


class N4PunchClient:
    """
    N4 打洞客户端
    负责与信号服务器交互(EXCHANGE)以及与对端进行物理打洞(PUNCH)。
    注意：在此阶段保持 Socket 为普通 UDP，不设置 QoS，以确保握手包的高通过率。
    """
    def __init__(self,
                 ident: bytes,
                 server_host: str,
                 server_port: int,
                 src_port_start: int = 30000,
                 src_port_count: int = 25,
                 peer_port_offset: int = 20,
                 allow_cross_ip: bool = True) -> None:
        self.ident = ident[:6].ljust(6, b'\x00') if len(ident) < 6 else ident[:6]
        self.server_host = server_host
        self.server_port = server_port
        self.src_port_start = src_port_start
        self.src_port_count = src_port_count
        self.peer_port_offset = peer_port_offset
        self.allow_cross_ip = allow_cross_ip
        self.pool: List[socket.socket] = []
        self.logger = LoggerFactory.get_logger()
        self._running = True

    def _init_sock_pool(self) -> None:
        """初始化 UDP Socket 池"""
        self.pool = []
        start_port = self.src_port_start

        for attempt in range(5):
            try:
                temp_pool = []
                for i in range(self.src_port_count):
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

                    # [关键配置]
                    # 1. 端口复用，便于后续 KCP 接管
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    except (AttributeError, OSError):
                        pass

                    # 2. 绑定端口
                    sock.bind(('0.0.0.0', start_port + i))

                    # 3. 设置非阻塞
                    sock.setblocking(False)

                    # [注意] 此处不设置 IP_TOS，保持为普通 UDP 流量
                    # 确保向 19999 端口的 EXCHANGE 包不被拦截

                    temp_pool.append(sock)
                self.pool = temp_pool
                self.logger.info(f"N4Punch: Created {len(self.pool)} sockets (ports {start_port}-{start_port + self.src_port_count - 1})")
                return
            except OSError as e:
                self.logger.debug(f"N4Punch: Port range {start_port} failed: {e}, trying next range")
                for s in temp_pool:
                    try: s.close()
                    except: pass
                start_port += 100

        raise N4Error.PunchFailure(f"Failed to create socket pool after 5 attempts")

    def _close_pool_except(self, winner: Optional[socket.socket] = None) -> None:
        """关闭池中除胜出者以外的所有 Socket"""
        for s in self.pool:
            if s != winner:
                try: s.close()
                except: pass
        self.pool = []

    def stop(self) -> None:
        self._running = False
        self._close_pool_except()

    def send_exchange(self) -> socket.socket:
        """
        发送 EXCHANGE 握手包到中继服务器 (端口 19999)。
        使用普通 UDP 包，确保通过防火墙。
        """
        if not self.pool:
            self._init_sock_pool()

        exchg_pkt = N4Packet.exchange(self.ident)

        success_count = 0
        # 增加发送频次，防止单次丢包
        for _ in range(5):
            try:
                # 使用第一个 socket 发送
                self.pool[0].sendto(exchg_pkt, (self.server_host, self.server_port))
                success_count += 1
            except Exception as e:
                self.logger.debug(f"N4Punch: EXCHANGE send error: {e}")
            time.sleep(0.05)

        if success_count > 0:
            self.logger.info(f"N4Punch: Sent EXCHANGE to {self.server_host}:{self.server_port} (Normal UDP)")
        else:
            self.logger.error("N4Punch: Failed to send EXCHANGE")

        return self.pool[0]

    def punch(self, peer_ip: str, peer_port: int, wait: int = 15) -> Tuple[socket.socket, Tuple[str, int]]:
        """执行 P2P 打洞"""
        if not self.pool:
            self._init_sock_pool()

        targets = set()
        targets.add((peer_ip, peer_port))
        # 探测端口范围，应对对称 NAT
        for i in range(1, self.peer_port_offset + 1):
            if peer_port + i <= 65535: targets.add((peer_ip, peer_port + i))
            if peer_port - i > 0: targets.add((peer_ip, peer_port - i))

        self.logger.info(f"N4Punch: Starting punch to {peer_ip}:{peer_port}")

        punch_pkt = N4Packet.punch(self.ident)

        # 1. 初始爆发：向对端发送 PUNCH 包 (普通 UDP)
        for _ in range(3):
            if not self._running:
                raise N4Error.PunchFailure("Punch process stopped")
            for sock in self.pool:
                for target in targets:
                    try: sock.sendto(punch_pkt, target)
                    except: pass
            time.sleep(0.02)

        # 2. 监听回复
        start_time = time.time()
        while self._running and (time.time() - start_time) < wait:
            # 持续低频发送保活
            for sock in self.pool:
                for target in targets:
                    try: sock.sendto(punch_pkt, target)
                    except: pass

            try:
                # 监听是否有数据发回来
                r, _, _ = select.select(self.pool, [], [], 0.5)
            except:
                continue

            if not r: continue

            for sock in r:
                try:
                    data, recv_peer = sock.recvfrom(65536)
                    recv_ident = N4Packet.dec_punch(data)

                    # 验证身份
                    if recv_ident and recv_ident.rstrip(b'\x00') == self.ident.rstrip(b'\x00'):
                        if recv_peer[0] == peer_ip or self.allow_cross_ip:
                            self.logger.info(f"N4Punch: !!! WINNER !!! Received PUNCH from {recv_peer}")

                            # 发送确认包帮助对方打通
                            for _ in range(10):
                                try: sock.sendto(punch_pkt, recv_peer)
                                except: pass
                                time.sleep(0.01)

                            # 选中这个 Socket，保留不关闭，交给 Tunnel 使用
                            self._close_pool_except(sock)
                            sock.setblocking(False)
                            return sock, recv_peer
                except Exception:
                    pass

        self._close_pool_except()
        raise N4Error.PunchFailure(f"UDP punch timed out after {wait}s")

    def get_local_port(self) -> Optional[int]:
        if self.pool:
            try: return self.pool[0].getsockname()[1]
            except: pass
        return None