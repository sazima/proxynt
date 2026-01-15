import logging
import socket
import select
import threading
import time
import struct
from collections import defaultdict
from typing import Dict, Optional, Callable, List, Tuple

from common.logger_factory import LoggerFactory
from common.n4_protocol import N4Packet
from client.tunnel_protocol import TunnelPacket
from common.kcp import Kcp

# ==========================================
# 1. KCP 适配器
# ==========================================
class N4Kcp(Kcp):
    def __init__(self, conv, socket_sender):
        super().__init__(conv)
        self.socket_sender = socket_sender

    def output(self, buf):
        self.socket_sender(buf)

def get_pair_key(client_a: str, client_b: str) -> Tuple[str, str]:
    return (min(client_a, client_b), max(client_a, client_b))

# ==========================================
# 2. 复刻 n4.py 的核心打洞器 (线程安全版)
# ==========================================
class N4Puncher:
    """
    负责执行 UDP 打洞流程：
    - 创建 Socket 池
    - 发送 Exchange
    - 收到 Peer Info 后猛烈发包
    - 锁定胜出的连接
    """
    def __init__(self,
                 local_client_name: str,
                 peer_name: str,
                 server_host: str,
                 server_port: int,
                 session_id: bytes):
        self.local_client_name = local_client_name
        self.peer_name = peer_name
        self.server_host = server_host
        self.server_port = server_port
        self.session_id = session_id

        # N4 配置
        self.src_port_start = 30000
        self.src_port_count = 25
        self.peer_port_offset = 20
        self.pool: List[socket.socket] = []

        self.running = True
        self.logger = LoggerFactory.get_logger()

    def _init_sock_pool(self):
        """初始化 Socket 池"""
        self.pool = []
        start_port = self.src_port_start
        # 尝试寻找可用端口段
        for _ in range(5):
            try:
                temp_pool = []
                for i in range(self.src_port_count):
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        if hasattr(socket, "SO_REUSEPORT"):
                            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    except:
                        pass
                    sock.bind(('0.0.0.0', start_port + i))
                    sock.setblocking(False)
                    temp_pool.append(sock)
                self.pool = temp_pool
                self.logger.info(f"Created {len(self.pool)} sockets for punch to {self.peer_name}")
                return
            except OSError:
                start_port += 100
                for s in temp_pool: s.close()

        raise Exception("Failed to create socket pool")

    def stop(self):
        self.running = False
        for s in self.pool:
            try: s.close()
            except: pass
        self.pool = []

    def punch(self, peer_ip: str, peer_port: int) -> Tuple[socket.socket, Tuple[str, int]]:
        """执行打洞流程，返回 (胜出的Socket, 真实的Peer地址)"""
        if not self.pool:
            try:
                self._init_sock_pool()
            except Exception as e:
                self.logger.error(f"Init pool failed: {e}")
                return None, None

        # 1. 向 Signal Server 发送 Exchange
        exchg_pkt = N4Packet.exchange(self.session_id)
        for _ in range(3):
            if self.pool:
                try: self.pool[0].sendto(exchg_pkt, (self.server_host, self.server_port))
                except: pass
            time.sleep(0.05)

        self.logger.info(f"Sent EXCHANGE. Target Peer: {peer_ip}:{peer_port}")

        # 2. 准备打洞目标 (包含端口预测)
        target_base = (peer_ip, peer_port)
        targets = set()
        targets.add(target_base)
        for i in range(1, self.peer_port_offset + 1):
            if peer_port + i <= 65535: targets.add((peer_ip, peer_port + i))
            if peer_port - i > 0: targets.add((peer_ip, peer_port - i))

        punch_pkt = N4Packet.punch(self.session_id)

        # 3. 猛烈发包
        self.logger.info(f"Punching {self.peer_name}...")

        # 先发几轮探测
        for _ in range(5):
            if not self.running: return None, None
            for sock in self.pool:
                for t in targets:
                    try: sock.sendto(punch_pkt, t)
                    except: pass
            time.sleep(0.1)

        # 4. 监听回复 (Winner Takes All)
        start_time = time.time()
        # 15秒打洞超时
        while self.running and time.time() - start_time < 15:
            # 持续发包保持 NAT 映射
            for sock in self.pool:
                for t in targets:
                    try: sock.sendto(punch_pkt, t)
                    except: pass

            r, _, _ = select.select(self.pool, [], [], 0.5)

            if not r:
                continue

            for sock in r:
                try:
                    data, addr = sock.recvfrom(65536)
                    recv_ident = N4Packet.dec_punch(data)

                    if recv_ident == self.session_id:
                        self.logger.info(f"!!! WINNER !!! PUNCH from {addr} (Socket fd: {sock.fileno()})")

                        winning_sock = sock
                        winning_addr = addr

                        self._cleanup_losers(winning_sock)

                        # 疯狂回包建立信任，防止对方不知道我们已经通了
                        for _ in range(10):
                            try: winning_sock.sendto(punch_pkt, winning_addr)
                            except: pass
                            time.sleep(0.02)

                        return winning_sock, winning_addr
                except Exception:
                    pass

        return None, None

    def _cleanup_losers(self, winner):
        new_pool = []
        for s in self.pool:
            if s != winner:
                try: s.close()
                except: pass
            else:
                new_pool.append(s)
        self.pool = new_pool

# ==========================================
# 3. 隧道对象
# ==========================================
class N4Tunnel:
    def __init__(self, pair_key, local_name, peer_name, socket, addr, session_id):
        self.pair_key = pair_key
        self.local_name = local_name
        self.peer_name = peer_name
        self.session_id = session_id

        self.socket = socket      # 胜出的 Socket
        self.peer_addr = addr     # 真实的 Peer 地址
        self.verified = False     # 是否双向验证 (收到 Keepalive ACK 才算)

        self.last_activity = time.time()
        self.lock = threading.Lock()

        # [修复] 补全心跳相关属性，防止 AttributeError
        self.last_keepalive_sent = 0
        self.keepalive_pending = False
        self.keepalive_fails = 0

        # KCP 配置
        self.kcp = N4Kcp(conv=1, socket_sender=self._send_udp)
        self.kcp.stream = True
        self.kcp.nodelay(1, 10, 2, 1)
        self.kcp.wndsize(128, 128)
        self.kcp.setmtu(1350) # 略小于 1400 防止 UDP 分片

        self.stream_buffer = bytearray()

    def _send_udp(self, data):
        try:
            self.socket.sendto(data, self.peer_addr)
        except:
            pass

    def send_kcp(self, data: bytes):
        with self.lock:
            self.kcp.send(data)
            self.kcp.flush()
            # 立即驱动一次
            current = int(time.time() * 1000) & 0xffffffff
            self.kcp.update(current)
        self.last_activity = time.time()

    def close(self):
        try: self.socket.close()
        except: pass

# ==========================================
# 4. 管理器 (集成到 proxynt)
# ==========================================
class N4TunnelManager:
    def __init__(self, local_client_name: str, server_host: str, server_port: int):
        self.local_client_name = local_client_name
        self.server_host = server_host
        self.server_port = server_port

        # 存储已建立的隧道
        self.tunnels: Dict[Tuple[str, str], N4Tunnel] = {}
        # 存储正在进行的打洞任务
        self.punchers: Dict[Tuple[str, str], N4Puncher] = {}
        # UID 映射
        self.uid_to_peer: Dict[bytes, str] = {}

        # 记录正在请求打洞的 Peer，防止重复请求
        self.pending_punch_requests: set = set()

        self.lock = threading.Lock()
        self.running = True

        self.on_data_received = None
        self.on_tunnel_closed = None
        self.on_punch_failed = None # 兼容旧代码调用

        self.ws_client = None

    def set_ws_client(self, ws):
        self.ws_client = ws

    def start(self):
        self.running = True
        # 启动主循环线程 (处理数据接收、心跳、KCP Tick)
        threading.Thread(target=self._main_loop, daemon=True).start()
        LoggerFactory.get_logger().info("N4 Tunnel Manager Started")

    def stop(self):
        self.running = False
        with self.lock:
            for t in self.tunnels.values(): t.close()
            for p in self.punchers.values(): p.stop()
            self.tunnels.clear()
            self.punchers.clear()

    # --- 外部调用接口 (被 run_client.py 调用) ---

    def register_uid(self, uid: bytes, peer_name: str):
        with self.lock:
            self.uid_to_peer[uid] = peer_name

    def unregister_uid(self, uid: bytes):
        with self.lock:
            self.uid_to_peer.pop(uid, None)

    # [修复] 补充 run_client.py 需要的方法
    def is_tunnel_established(self, peer_name: str) -> bool:
        """检查隧道是否已建立 (在 tunnels 列表中)"""
        pair_key = get_pair_key(self.local_client_name, peer_name)
        with self.lock:
            return pair_key in self.tunnels

    # [修复] 补充 run_client.py 需要的方法
    def has_tunnel(self, peer_name: str) -> bool:
        """检查是否有隧道 (包括正在建立的)"""
        pair_key = get_pair_key(self.local_client_name, peer_name)
        with self.lock:
            return (pair_key in self.tunnels) or (pair_key in self.punchers)

    def prepare_punch(self, peer_name: str, session_id: bytes):
        """收到 Server 的 PUNCH_REQUEST 后调用"""
        pair_key = get_pair_key(self.local_client_name, peer_name)

        with self.lock:
            # === 关键修改：对方请求打洞，说明对方可能重启了 ===
            # 即使我们本地有隧道，也认为是脏数据，必须强制清除，以便建立新连接
            if pair_key in self.tunnels:
                LoggerFactory.get_logger().warn(f"Peer {peer_name} requested new punch. Destroying old tunnel.")
                old_tunnel = self.tunnels[pair_key]
                old_tunnel.close()
                del self.tunnels[pair_key]

            # 检查是否正在打洞中，如果是，则忽略重复请求
            if pair_key in self.punchers:
                return None

            puncher = N4Puncher(self.local_client_name, peer_name,
                                self.server_host, self.server_port, session_id)
            try:
                puncher._init_sock_pool()
                self.punchers[pair_key] = puncher
                return puncher.pool[0]
            except:
                return None

    def receive_peer_info(self, peer_name: str, peer_ip: str, peer_port: int):
        """收到 Server 的 PEER_INFO，启动打洞线程"""
        pair_key = get_pair_key(self.local_client_name, peer_name)
        with self.lock:
            puncher = self.punchers.get(pair_key)

        if puncher:
            # 启动独立线程去执行耗时的打洞操作
            threading.Thread(
                target=self._run_punch_task,
                args=(puncher, pair_key, peer_ip, peer_port),
                daemon=True
            ).start()

    def _run_punch_task(self, puncher: N4Puncher, pair_key, ip, port):
        """独立的打洞线程"""
        sock, addr = puncher.punch(ip, port)

        with self.lock:
            # 无论成功失败，都从 punchers 中移除
            if pair_key in self.punchers:
                del self.punchers[pair_key]

            # 清除 pending 标记，允许再次请求
            if puncher.peer_name in self.pending_punch_requests:
                self.pending_punch_requests.remove(puncher.peer_name)

            if sock and addr:
                # 打洞成功，创建隧道对象
                tunnel = N4Tunnel(pair_key, self.local_client_name, puncher.peer_name, sock, addr, puncher.session_id)
                tunnel.verified = True
                self.tunnels[pair_key] = tunnel

                # 立即发送 Keepalive 探测 (激活 Verified 状态)
                try:
                    tunnel.send_kcp(TunnelPacket.pack_keepalive())
                except: pass
            else:
                LoggerFactory.get_logger().warn(f"Punch failed for {puncher.peer_name}, traffic will stay on WebSocket")

    def send_data(self, uid: bytes, data: bytes) -> bool:
        """
        发送数据 (核心混合模式逻辑)
        返回 True: 数据已通过 P2P 隧道发送
        返回 False: 数据未发送，请调用者使用 WebSocket 发送
        """
        peer_name = self.uid_to_peer.get(uid)
        if not peer_name: return False

        pair_key = get_pair_key(self.local_client_name, peer_name)

        # 1. 尝试获取隧道
        tunnel = None
        with self.lock:
            tunnel = self.tunnels.get(pair_key)

        # 2. 隧道不存在或未验证 -> 自动降级为 WebSocket 并尝试建立隧道
        if not tunnel or not tunnel.verified:
            # 如果没有隧道，也没有正在进行的打洞请求，且 ws 可用 -> 触发打洞
            if not tunnel and self.ws_client and peer_name not in self.pending_punch_requests:
                with self.lock:
                    # 再次检查 punchers 防止并发
                    is_punching = pair_key in self.punchers

                if not is_punching:
                    LoggerFactory.get_logger().info(f"Auto-triggering P2P setup for {peer_name}")
                    self.pending_punch_requests.add(peer_name)
                    # 调用 WebSocketClient 发送请求
                    self.ws_client._send_punch_request(peer_name)

            # 如果隧道存在但未验证 (单向通)，发送心跳催促
            if tunnel and not tunnel.verified:
                # 限制频率 1秒一次
                if time.time() - tunnel.last_activity > 1.0:
                    tunnel.send_kcp(TunnelPacket.pack_keepalive())

            return False # 返回 False，TcpForwardClient 会自动走 WebSocket

        # 3. 隧道已就绪 (Verified) -> 走 P2P (应用层分片防止 SFTP 崩溃)
        MAX_CHUNK = 1300
        if len(data) <= MAX_CHUNK:
            packet = TunnelPacket.pack_data(uid, data)
            tunnel.send_kcp(packet)
        else:
            offset = 0
            while offset < len(data):
                end = offset + MAX_CHUNK
                chunk = data[offset:end]
                packet = TunnelPacket.pack_data(uid, chunk)
                tunnel.send_kcp(packet)
                offset = end
        return True

    def close_uid(self, uid: bytes):
        peer_name = self.uid_to_peer.get(uid)
        if peer_name:
            pair_key = get_pair_key(self.local_client_name, peer_name)
            with self.lock:
                tunnel = self.tunnels.get(pair_key)
            if tunnel and tunnel.verified:
                tunnel.send_kcp(TunnelPacket.pack_close_uid(uid))
            self.unregister_uid(uid)

    def _main_loop(self):
        """主循环：处理所有隧道的接收、心跳、超时"""
        while self.running:
            with self.lock:
                active_tunnels = list(self.tunnels.values())

            if not active_tunnels:
                time.sleep(0.1)
                continue

            current_time_ms = int(time.time() * 1000) & 0xffffffff
            now = time.time()

            read_sockets = []
            sock_to_tunnel = {}

            for tunnel in active_tunnels:
                # 1. KCP Tick
                try:
                    with tunnel.lock:
                        tunnel.kcp.update(current_time_ms)
                except: pass

                # 2. 心跳保活逻辑 (按要求调整)
                # ----------------------------------------------------
                # 这里的 last_activity 包含收到数据、发送数据的时间
                if now - tunnel.last_activity > 10: # 10秒空闲
                    if not tunnel.keepalive_pending:
                        # 发送第一次探测
                        tunnel.send_kcp(TunnelPacket.pack_keepalive())
                        tunnel.last_keepalive_sent = now
                        tunnel.keepalive_pending = True
                        # LoggerFactory.get_logger().debug(f"Keepalive PING sent to {tunnel.peer_name}")

                    elif now - tunnel.last_keepalive_sent > 5: # 5秒没收到ACK
                        tunnel.keepalive_fails += 1
                        tunnel.keepalive_pending = False # 允许立即重发下一次

                        LoggerFactory.get_logger().warn(f"Keepalive timeout for {tunnel.peer_name} ({tunnel.keepalive_fails}/2)")

                        # 连续2次失败，认为断开
                        if tunnel.keepalive_fails >= 2:
                            LoggerFactory.get_logger().error(f"Tunnel to {tunnel.peer_name} DEAD (Heartbeat failed). Switching to WebSocket.")
                            self._close_tunnel(tunnel)
                            continue
                # ----------------------------------------------------

                # 兜底：如果60秒没有任何活动（包括心跳也发不出去的情况），强制关闭
                if now - tunnel.last_activity > 60:
                    self._close_tunnel(tunnel)
                    continue

                read_sockets.append(tunnel.socket)
                sock_to_tunnel[tunnel.socket] = tunnel

            if not read_sockets:
                time.sleep(0.01)
                continue

            # 3. 接收数据
            try:
                r, _, _ = select.select(read_sockets, [], [], 0.01)
                for sock in r:
                    tunnel = sock_to_tunnel.get(sock)
                    if not tunnel: continue

                    try:
                        data, addr = sock.recvfrom(65536)

                        # 处理 N4 PUNCH 确认包
                        if len(data) == 8:
                            pun = N4Packet.dec_punch(data)
                            if pun == tunnel.session_id:
                                if addr != tunnel.peer_addr:
                                    tunnel.peer_addr = addr
                                continue

                        # 收到有效业务数据或心跳包
                        if addr != tunnel.peer_addr:
                            tunnel.peer_addr = addr

                        self._input_kcp(tunnel, data)
                    except:
                        pass
            except:
                pass

    def _close_tunnel(self, tunnel):
        tunnel.close()
        with self.lock:
            self.tunnels.pop(tunnel.pair_key, None)
            # 清除 pending 标记，允许重新建立
            if tunnel.peer_name in self.pending_punch_requests:
                self.pending_punch_requests.remove(tunnel.peer_name)
        if self.on_tunnel_closed:
            self.on_tunnel_closed(tunnel.peer_name)

    def _input_kcp(self, tunnel, data):
        """将 UDP 数据输入 KCP 并取出完整包"""
        with tunnel.lock:
            try: tunnel.kcp.input(data)
            except: return

            tunnel.last_activity = time.time()

            # 读取流
            while True:
                chunk = tunnel.kcp.recv()
                if not chunk: break
                tunnel.stream_buffer.extend(chunk)

            # 解析 TunnelPacket
            HEADER = 8
            while len(tunnel.stream_buffer) >= HEADER:
                payload_len = (tunnel.stream_buffer[6] << 8) | tunnel.stream_buffer[7]
                total = HEADER + payload_len
                if len(tunnel.stream_buffer) >= total:
                    pkt = bytes(tunnel.stream_buffer[:total])
                    del tunnel.stream_buffer[:total]
                    self._handle_tunnel_packet(tunnel, pkt)
                else:
                    break

    def _handle_tunnel_packet(self, tunnel, packet):
        """处理解包后的数据"""
        try:
            res = TunnelPacket.unpack(packet)
            if not res: return

            pkt_type, flags, uid, payload = res

            if pkt_type == TunnelPacket.TYPE_DATA:
                if uid not in self.uid_to_peer:
                    self.register_uid(uid, tunnel.peer_name)
                if self.on_data_received:
                    self.on_data_received(uid, payload)

            elif pkt_type == TunnelPacket.TYPE_KEEPALIVE:
                tunnel.send_kcp(TunnelPacket.pack_keepalive_ack())

            elif pkt_type == TunnelPacket.TYPE_KEEPALIVE_ACK:
                tunnel.keepalive_pending = False
                tunnel.keepalive_fails = 0
                if not tunnel.verified:
                    tunnel.verified = True
                    LoggerFactory.get_logger().info(f"Tunnel to {tunnel.peer_name} VERIFIED (Ready for P2P)")

            elif pkt_type == TunnelPacket.TYPE_CLOSE_UID:
                self.unregister_uid(uid)

            elif pkt_type == TunnelPacket.TYPE_TUNNEL_CLOSE:
                self._close_tunnel(tunnel)

        except:
            pass