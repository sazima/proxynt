#!/usr/bin/env python3
"""
N4 UDP Hole Punching Client - Adapted for WebSocket coordination

Based on the original n4.py implementation, but modified to:
1. Work with WebSocket-based coordination (no TCP connection to N4 server)
2. Return the winning socket instead of closing it
3. Support async integration
"""

from typing import Optional, Tuple, List
import socket
import select
import time

from common.logger_factory import LoggerFactory
from common.n4_protocol import N4Packet, N4Error


class N4PunchClient:
    """
    UDP Hole Punching Client for P2P connections.

    This version is designed to work with WebSocket-based coordination:
    - No TCP connection to N4 server required
    - Peer info is provided externally (via WebSocket P2P_PEER_INFO message)
    - Returns the winning socket for subsequent QUIC handshake
    """

    def __init__(self,
                 ident: bytes,
                 server_host: str,
                 server_port: int,
                 src_port_start: int = 30000,
                 src_port_count: int = 25,
                 peer_port_offset: int = 20,
                 allow_cross_ip: bool = True) -> None:
        """
        Initialize the punch client.

        Args:
            ident: 6-byte identifier for this punch session
            server_host: N4 relay server hostname (for EXCHANGE packets)
            server_port: N4 relay server port
            src_port_start: Starting local port for socket pool
            src_port_count: Number of sockets in the pool
            peer_port_offset: Port range offset when punching
            allow_cross_ip: Allow punch from different IP than expected
        """
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
        """Initialize the UDP socket pool."""
        self.pool = []
        start_port = self.src_port_start

        # Try different port ranges if binding fails
        for attempt in range(5):
            try:
                temp_pool = []
                for i in range(self.src_port_count):
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    except (AttributeError, OSError):
                        pass
                    sock.bind(('0.0.0.0', start_port + i))
                    sock.setblocking(False)
                    temp_pool.append(sock)
                self.pool = temp_pool
                self.logger.info(f"N4Punch: Created {len(self.pool)} sockets (ports {start_port}-{start_port + self.src_port_count - 1})")
                return
            except OSError as e:
                self.logger.debug(f"N4Punch: Port range {start_port} failed: {e}, trying next range")
                for s in temp_pool:
                    try:
                        s.close()
                    except:
                        pass
                start_port += 100

        raise N4Error.PunchFailure(f"Failed to create socket pool after 5 attempts")

    def _close_pool_except(self, winner: Optional[socket.socket] = None) -> None:
        """Close all sockets in pool except the winner."""
        for s in self.pool:
            if s != winner:
                try:
                    s.close()
                except:
                    pass
        self.pool = []

    def stop(self) -> None:
        """Stop the punch process."""
        self._running = False
        self._close_pool_except()

    def send_exchange(self) -> socket.socket:
        """
        Initialize socket pool and send EXCHANGE packets to server.

        Returns:
            The first socket in the pool (used for EXCHANGE)
        """
        if not self.pool:
            self._init_sock_pool()

        exchg_pkt = N4Packet.exchange(self.ident)

        # Send EXCHANGE to server multiple times
        for _ in range(3):
            try:
                self.pool[0].sendto(exchg_pkt, (self.server_host, self.server_port))
            except Exception as e:
                self.logger.debug(f"N4Punch: EXCHANGE send error: {e}")
            time.sleep(0.05)

        self.logger.info(f"N4Punch: Sent EXCHANGE to {self.server_host}:{self.server_port}")
        return self.pool[0]

    def punch(self, peer_ip: str, peer_port: int, wait: int = 15) -> Tuple[socket.socket, Tuple[str, int]]:
        """
        执行 UDP 打洞流程。

        Args:
            peer_ip: 对端的公网 IP
            peer_port: 对端的公网端口
            wait: 打洞最长等待时间（秒）

        Returns:
            Tuple[winning_socket, (peer_ip, peer_port)]

        Raises:
            N4Error.PunchFailure: 打洞超时或失败
        """
        if not self.pool:
            self._init_sock_pool()

        # 1. 构建目标列表 (基于基准端口 ± offset 范围)
        targets = set()
        targets.add((peer_ip, peer_port))
        for i in range(1, self.peer_port_offset + 1):
            if peer_port + i <= 65535:
                targets.add((peer_ip, peer_port + i))
            if peer_port - i > 0:
                targets.add((peer_ip, peer_port - i))

        self.logger.info(f"N4Punch: Starting punch to {peer_ip}:{peer_port} (range ±{self.peer_port_offset})")

        punch_pkt = N4Packet.punch(self.ident)

        # 2. 初始爆发：尝试穿透 NAT
        for _ in range(3):
            if not self._running:
                raise N4Error.PunchFailure("Punch process stopped")
            for sock in self.pool:
                for target in targets:
                    try:
                        sock.sendto(punch_pkt, target)
                    except:
                        pass
            time.sleep(0.05)

        # 3. 核心监听与重试循环
        start_time = time.time()
        while self._running and (time.time() - start_time) < wait:
            # 持续向所有可能的目标发送 PUNCH 包
            for sock in self.pool:
                for target in targets:
                    try:
                        sock.sendto(punch_pkt, target)
                    except:
                        pass

            # 使用 select 监听 25 个 socket 的响应情况
            try:
                # 阻塞 0.5 秒以降低 CPU 占用，并处理接收
                r, _, _ = select.select(self.pool, [], [], 0.5)
            except:
                continue

            if not r:
                continue

            # 遍历有数据的 socket
            for sock in r:
                try:
                    data, recv_peer = sock.recvfrom(65536)

                    # 尝试解析是否为 N4 PUNCH 包
                    recv_ident = N4Packet.dec_punch(data)

                    # 验证会话标识符（忽略可能的填充字节）
                    if recv_ident and recv_ident.rstrip(b'\x00') == self.ident.rstrip(b'\x00'):
                        # 验证 IP 匹配（安全性检查，除非显式开启 allow_cross_ip）
                        if recv_peer[0] == peer_ip or self.allow_cross_ip:
                            self.logger.info(f"N4Punch: !!! WINNER !!! Received PUNCH from {recv_peer}")

                            # 4. 胜出后续操作：
                            # 连续发送少量确认包给对端，帮助对端也从 punch 循环中跳出
                            for _ in range(8):
                                try:
                                    sock.sendto(punch_pkt, recv_peer)
                                except:
                                    pass

                            # 立即关闭无效 Socket，并将胜出的 Socket 设置为非阻塞
                            self._close_pool_except(sock)
                            sock.setblocking(False)

                            return sock, recv_peer
                        else:
                            self.logger.debug(f"N4Punch: Ignored PUNCH from unexpected IP {recv_peer[0]}")
                except Exception as e:
                    self.logger.debug(f"N4Punch: Receive error on socket: {e}")

        # 如果循环结束仍未建立连接
        self._close_pool_except()
        raise N4Error.PunchFailure(f"UDP punch timed out after {wait}s")

    def get_local_port(self) -> Optional[int]:
        """Get the local port of the first socket in pool."""
        if self.pool:
            try:
                return self.pool[0].getsockname()[1]
            except:
                pass
        return None
