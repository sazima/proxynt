"""
N4 Tunnel Manager for P2P Communication

Features:
- Single tunnel per client pair (bidirectional)
- Multiplexed data over single UDP socket
- Persistent tunnels with idle timeout
- Automatic disconnect synchronization
"""
import logging
import socket
import select
import threading
import time
import traceback
from collections import defaultdict
from typing import Dict, Optional, Callable, List, Tuple

from common.logger_factory import LoggerFactory
from common.n4_protocol import N4Packet, N4Error
from client.tunnel_protocol import TunnelPacket
from common.kcp import Kcp
class N4Kcp(Kcp):
    def __init__(self, conv, socket_sender):
        super().__init__(conv)
        self.socket_sender = socket_sender

    # 【关键】KCP 库通过调用这个方法把数据发给 UDP
    def output(self, buf):
        # buf 是 bytes 类型
        self.socket_sender(buf)
def get_pair_key(client_a: str, client_b: str) -> Tuple[str, str]:
    """Generate consistent tunnel key regardless of direction"""
    return (min(client_a, client_b), max(client_a, client_b))


class N4Tunnel:
    """
    Represents a single P2P tunnel to a peer.

    The tunnel is bidirectional - both A->B and B->A use the same tunnel.
    """

    def __init__(self, pair_key: Tuple[str, str], local_name: str, peer_name: str):
        self.pair_key = pair_key
        self.local_name = local_name
        self.peer_name = peer_name

        # Socket pool for hole punching (from N4 strategy)
        self.socket_pool: List[socket.socket] = []

        # Active socket after punch success
        self.active_socket: Optional[socket.socket] = None
        self.peer_addr: Optional[Tuple[str, int]] = None

        # Pending peer info (received before punching starts)
        self.pending_peer_ip: Optional[str] = None
        self.pending_peer_port: Optional[int] = None

        # State: idle, punching, established, closing
        self.status: str = 'idle'
        self.last_activity: float = time.time()

        # Health check state
        self.last_keepalive_sent: float = 0  # Time when last keepalive was sent
        self.keepalive_pending: bool = False  # Waiting for keepalive response
        self.keepalive_failures: int = 0  # Consecutive keepalive failures
        self.max_keepalive_failures: int = 2  # Max failures before marking dead
        self.punch_start_time: float = 0  # Time when punching started

        # N4 hole punching configuration
        self.src_port_start: int = 30000
        self.src_port_count: int = 25
        self.peer_port_offset: int = 20

        # Session identifier for N4 protocol (6 bytes)
        self.session_id: bytes = b'\x00' * 6

        # Lock for thread safety
        self.lock = threading.Lock()

        # --- KCP 集成 ---
        # conv 这里暂时硬编码为 1，实际场景最好协商
        self.kcp = N4Kcp(conv=1, socket_sender=self._raw_udp_send)
        # [修改] 开启流模式，这很重要！
        self.kcp.stream = True
        self.kcp.nodelay(1, 10, 2, 1)
        self.kcp.wndsize(128, 128)
        # self.kcp.setmtu(1350)
        self.kcp.setmtu(1200)

        # [新增] 这里必须加上这个 buffer！
        self.stream_buffer = bytearray()


    def _raw_udp_send(self, data):
        """KCP 回调：发送 UDP 原始包"""
        if self.active_socket and self.peer_addr:
            try:
                self.active_socket.sendto(data, self.peer_addr)
            except OSError:
                pass

    def is_established(self) -> bool:
        return self.status == 'established' and self.active_socket is not None

    def send(self, data: bytes) -> bool:
        """
        上层业务调用发送
        """
        if not self.is_established():
            return False

        with self.lock:
            # 1. 将数据塞入 KCP 发送队列
            self.kcp.send(data)
            self.kcp.flush()

            # 2. 立即驱动一次 KCP (flush)，减少延迟
            # 注意：pykcp 的 update 需要传入当前毫秒时间戳
            current = int(time.time() * 1000) & 0xffffffff
            self.kcp.update(current)

        self.last_activity = time.time()
        return True

    def send_keepalive(self) -> bool:
        """Send keepalive packet"""
        return self.send(TunnelPacket.pack_keepalive())

    def close(self):
        """Close all sockets"""
        with self.lock:
            for sock in self.socket_pool:
                try:
                    sock.close()
                except:
                    pass
            self.socket_pool.clear()

            if self.active_socket:
                try:
                    self.active_socket.close()
                except:
                    pass
                self.active_socket = None

            self.status = 'closing'


class N4TunnelManager:
    """
    Manages P2P tunnels using N4 hole punching protocol.

    Key Features:
    - Single tunnel per client pair (bidirectional)
    - Tunnel key: (min(a,b), max(a,b))
    - Multiplexed data over single UDP socket
    - Persistent tunnels with idle timeout
    """

    def __init__(self, local_client_name: str, server_host: str, server_port: int):
        self.local_client_name = local_client_name
        self.server_host = server_host
        self.server_port = server_port

        # Tunnels indexed by pair_key: (min(a,b), max(a,b)) -> N4Tunnel
        self.tunnels: Dict[Tuple[str, str], N4Tunnel] = {}

        # UID routing: uid -> peer_name
        self.uid_to_peer: Dict[bytes, str] = {}

        # Global lock
        self.lock = threading.Lock()

        # Running state
        self.running = False
        self.receive_thread: Optional[threading.Thread] = None
        self.monitor_thread: Optional[threading.Thread] = None

        # Current bind port for socket pool (sequential)
        self.current_bind_port = 30000

        # Callbacks
        self.on_data_received: Optional[Callable[[bytes, bytes], None]] = None
        self.on_tunnel_closed: Optional[Callable[[str], None]] = None
        self.on_punch_failed: Optional[Callable[[str], None]] = None  # Called when punch times out

        # Punch timeout (seconds) - if punching takes longer, fall back to WebSocket
        self.punch_timeout: float = 10.0

        # WebSocket client reference (for sending messages)
        self.ws_client = None
        # 用于缓存接收到的分片数据: uid -> bytearray
        self.reassembly_buffers: Dict[bytes, bytearray] = defaultdict(bytearray)

    def set_ws_client(self, ws_client):
        """Set WebSocket client reference"""
        self.ws_client = ws_client


    def _kcp_tick_loop(self):
        """KCP 驱动线程"""
        while self.running:
            try:
                # 获取当前毫秒时间戳 (ctypes 需要 int)
                current_time = int(time.time() * 1000) & 0xffffffff

                with self.lock:
                    # 复制一份，避免遍历时字典变化
                    active_tunnels = [t for t in self.tunnels.values() if t.is_established()]

                for tunnel in active_tunnels:
                    # 加锁保护每个 tunnel 的 update
                    try:
                        if hasattr(tunnel, 'kcp'):
                            with tunnel.lock:
                                tunnel.kcp.update(current_time)
                    except Exception as e:
                        # 捕获单个 tunnel 的错误，不影响其他
                        LoggerFactory.get_logger().error(f"KCP update error for {tunnel.peer_name}: {e}")
            except Exception as e:
                # 捕获外层错误，防止线程退出
                LoggerFactory.get_logger().error(f"KCP tick loop error: {e}")

            # 10ms 间隔
            time.sleep(0.01)

    def start(self):
        """Start the tunnel manager"""
        if self.running:
            return
        self.running = True

        # Start receive loop
        self.receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.receive_thread.start()

        # Start monitor loop
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

        LoggerFactory.get_logger().info(f'N4 Tunnel Manager started for {self.local_client_name}')
        # --- 启动 KCP Tick 线程 ---
        self.tick_thread = threading.Thread(target=self._kcp_tick_loop, daemon=True)
        self.tick_thread.start()

        LoggerFactory.get_logger().info(f'N4 Tunnel Manager started...')

    def stop(self):
        """Stop the tunnel manager"""
        self.running = False

        with self.lock:
            for tunnel in self.tunnels.values():
                tunnel.close()
            self.tunnels.clear()
            self.uid_to_peer.clear()

        LoggerFactory.get_logger().info('N4 Tunnel Manager stopped')

    def get_tunnel(self, peer_name: str) -> Optional[N4Tunnel]:
        """Get existing tunnel to peer (if established)"""
        pair_key = get_pair_key(self.local_client_name, peer_name)
        with self.lock:
            tunnel = self.tunnels.get(pair_key)
            if tunnel and tunnel.is_established():
                return tunnel
        return None

    def has_tunnel(self, peer_name: str) -> bool:
        """Check if tunnel to peer exists (any state)"""
        pair_key = get_pair_key(self.local_client_name, peer_name)
        with self.lock:
            return pair_key in self.tunnels

    def is_tunnel_established(self, peer_name: str) -> bool:
        """Check if tunnel to peer is established"""
        tunnel = self.get_tunnel(peer_name)
        return tunnel is not None and tunnel.is_established()

    def is_tunnel_failed(self, peer_name: str) -> bool:
        """Check if tunnel to peer has failed (should use WebSocket instead)"""
        pair_key = get_pair_key(self.local_client_name, peer_name)
        with self.lock:
            tunnel = self.tunnels.get(pair_key)
            if tunnel:
                return tunnel.status == 'failed'
        return False

    def is_tunnel_punching(self, peer_name: str) -> bool:
        """Check if tunnel to peer is currently punching"""
        pair_key = get_pair_key(self.local_client_name, peer_name)
        with self.lock:
            tunnel = self.tunnels.get(pair_key)
            if tunnel:
                return tunnel.status == 'punching'
        return False

    def clear_failed_tunnel(self, peer_name: str):
        """Clear a failed tunnel so it can be retried later"""
        pair_key = get_pair_key(self.local_client_name, peer_name)
        with self.lock:
            tunnel = self.tunnels.get(pair_key)
            if tunnel and tunnel.status == 'failed':
                tunnel.close()
                self.tunnels.pop(pair_key, None)
                LoggerFactory.get_logger().info(f"Cleared failed tunnel to {peer_name}")

    def register_uid(self, uid: bytes, peer_name: str):
        """Register a UID to use a tunnel (for multiplexing)"""
        with self.lock:
            self.uid_to_peer[uid] = peer_name

    def unregister_uid(self, uid: bytes):
        """Unregister a UID"""
        with self.lock:
            self.uid_to_peer.pop(uid, None)

    def prepare_punch(self, peer_name: str, session_id: bytes) -> Optional[socket.socket]:
        """
        Prepare for hole punching by creating socket pool.
        Returns the first socket for sending EXCHANGE packet.
        """
        pair_key = get_pair_key(self.local_client_name, peer_name)

        with self.lock:
            # Check if tunnel already exists
            existing = self.tunnels.get(pair_key)
            if existing:
                if existing.is_established():
                    LoggerFactory.get_logger().info(f"Tunnel to {peer_name} already established")
                    return None
                if existing.status == 'punching':
                    LoggerFactory.get_logger().info(f"Tunnel to {peer_name} already punching")
                    return existing.socket_pool[0] if existing.socket_pool else None

            # Create new tunnel
            tunnel = N4Tunnel(pair_key, self.local_client_name, peer_name)
            tunnel.session_id = session_id
            tunnel.status = 'punching'
            tunnel.punch_start_time = time.time()  # Track when punching started

            # Create socket pool
            self._create_socket_pool(tunnel)

            self.tunnels[pair_key] = tunnel

            LoggerFactory.get_logger().info(
                f"Prepared punch for {peer_name}, session={session_id.hex()}, "
                f"{len(tunnel.socket_pool)} sockets created"
            )

            if tunnel.socket_pool:
                return tunnel.socket_pool[0]
            return None

    def receive_peer_info(self, peer_name: str, peer_ip: str, peer_port: int):
        """
        Called when server sends PEER_INFO after EXCHANGE phase.
        Starts the actual hole punching.
        """
        pair_key = get_pair_key(self.local_client_name, peer_name)

        with self.lock:
            tunnel = self.tunnels.get(pair_key)
            if not tunnel:
                LoggerFactory.get_logger().warning(f"No tunnel found for peer {peer_name}")
                return

            tunnel.pending_peer_ip = peer_ip
            tunnel.pending_peer_port = peer_port

        # Start punch thread
        threading.Thread(
            target=self._punch_loop,
            args=(tunnel, peer_ip, peer_port),
            daemon=True
        ).start()

    # ... 在 N4TunnelManager 类中 ...

    def send_data(self, uid: bytes, data: bytes) -> bool:
        """
        Send data via tunnel.
        Returns False if tunnel not ready or failed.
        """
        peer_name = self.uid_to_peer.get(uid)
        if not peer_name:
            return False

        pair_key = get_pair_key(self.local_client_name, peer_name)
        with self.lock:
            tunnel = self.tunnels.get(pair_key)

        if not tunnel:
            return False

        if tunnel.status == 'failed':
            return False

        if tunnel.status == 'punching':
            elapsed = time.time() - tunnel.punch_start_time
            if elapsed > self.punch_timeout:
                tunnel.status = 'failed'
                return False
            return False

        if not tunnel.is_established():
            return False

        # === 修改核心逻辑：强制应用层分片 ===
        # TunnelPacket 的 Header 中 length 字段是 unsigned short (max 65535)
        # 为了传输稳定性，我们将其切分为 MTU 安全的大小 (例如 1300-1400 字节)
        # 这样 KCP 不需要处理过大的 Frame，且能避免 struct.pack 溢出

        MAX_CHUNK_SIZE = 1350  # 留出 50 字节给 UDP头 + KCP头 + TunnelHeader

        if len(data) <= MAX_CHUNK_SIZE:
            # 数据较小，直接发送
            packet = TunnelPacket.pack_data(uid, data)
            return tunnel.send(packet)
        else:
            # 数据较大，切片发送
            # SSH 协议是流式的，我们只要按顺序把字节塞进去，接收端拼起来就行
            offset = 0
            total_len = len(data)
            success = True

            while offset < total_len:
                end = offset + MAX_CHUNK_SIZE
                chunk = data[offset:end]

                # 封装包
                packet = TunnelPacket.pack_data(uid, chunk)

                # 发送
                if not tunnel.send(packet):
                    success = False
                    break

                offset = end

            return success

    def close_uid(self, uid: bytes):
        """Close a specific UID stream (but keep tunnel alive)"""
        peer_name = self.uid_to_peer.get(uid)
        if not peer_name:
            return

        tunnel = self.get_tunnel(peer_name)
        if tunnel:
            # Send close packet for this UID
            packet = TunnelPacket.pack_close_uid(uid)
            tunnel.send(packet)

        self.unregister_uid(uid)

    def close_tunnel(self, peer_name: str, notify_peer: bool = True):
        """Close tunnel to peer and optionally notify the other end"""
        pair_key = get_pair_key(self.local_client_name, peer_name)

        with self.lock:
            tunnel = self.tunnels.get(pair_key)
            if not tunnel:
                return

            if notify_peer and tunnel.is_established():
                # Send tunnel close packet
                try:
                    packet = TunnelPacket.pack_tunnel_close()
                    tunnel.active_socket.sendto(packet, tunnel.peer_addr)
                except:
                    pass

            tunnel.close()
            self.tunnels.pop(pair_key, None)

            # Remove all UIDs for this peer
            uids_to_remove = [
                uid for uid, name in self.uid_to_peer.items()
                if name == peer_name
            ]
            for uid in uids_to_remove:
                self.uid_to_peer.pop(uid, None)
                self.reassembly_buffers.pop(uid, None) # <--- 新增：清理缓存

        LoggerFactory.get_logger().info(f"Tunnel to {peer_name} closed")

        if self.on_tunnel_closed:
            self.on_tunnel_closed(peer_name)

    def _create_socket_pool(self, tunnel: N4Tunnel):
        """Create socket pool for hole punching"""
        count = 0
        while count < tunnel.src_port_count:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    if hasattr(socket, "SO_REUSEPORT"):
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except:
                    pass

                sock.bind(('0.0.0.0', self.current_bind_port))
                sock.setblocking(False)
                tunnel.socket_pool.append(sock)
                count += 1
            except OSError:
                pass
            finally:
                self.current_bind_port += 1
                if self.current_bind_port > 60000:
                    self.current_bind_port = 30000

    def _punch_loop(self, tunnel: N4Tunnel, peer_ip: str, peer_port: int):
        """
        Execute N4 hole punching.
        Sends PUNCH packets to peer's predicted ports.
        """
        LoggerFactory.get_logger().info(
            f"Starting punch to {tunnel.peer_name} at {peer_ip}:{peer_port}"
        )

        punch_pkt = N4Packet.punch(tunnel.session_id)
        max_rounds = 60
        round_count = 0

        while self.running and tunnel.status == 'punching' and round_count < max_rounds:
            try:
                # Calculate target ports (peer_port +/- offset)
                target_ports = set()
                target_ports.add(peer_port)
                for i in range(1, tunnel.peer_port_offset + 1):
                    if peer_port + i <= 65535:
                        target_ports.add(peer_port + i)
                    if peer_port - i > 0:
                        target_ports.add(peer_port - i)

                # Send PUNCH packets from all sockets
                for sock in tunnel.socket_pool:
                    for port in target_ports:
                        try:
                            sock.sendto(punch_pkt, (peer_ip, port))
                        except OSError:
                            pass

                if round_count % 10 == 0:
                    LoggerFactory.get_logger().debug(
                        f"Punching {tunnel.peer_name}: round {round_count}/{max_rounds}"
                    )

                time.sleep(0.5)
                round_count += 1

                # Check if established by receive loop
                if tunnel.status == 'established':
                    return

            except Exception as e:
                LoggerFactory.get_logger().error(f"Punch loop error: {e}")
                break

        if tunnel.status != 'established':
            LoggerFactory.get_logger().warning(
                f"Hole punching failed for {tunnel.peer_name} after {round_count} rounds"
            )
            # Cleanup failed tunnel
            with self.lock:
                if tunnel.pair_key in self.tunnels:
                    self.tunnels.pop(tunnel.pair_key)
            tunnel.close()

    def _receive_loop(self):
        """Listen on all tunnel sockets for incoming data"""
        while self.running:
            try:
                sockets_map = {}  # socket -> tunnel
                read_list = []

                with self.lock:
                    for tunnel in self.tunnels.values():
                        if tunnel.is_established() and tunnel.active_socket:
                            read_list.append(tunnel.active_socket)
                            sockets_map[tunnel.active_socket] = tunnel
                        elif tunnel.status == 'punching':
                            for sock in tunnel.socket_pool:
                                read_list.append(sock)
                                sockets_map[sock] = tunnel

                if not read_list:
                    time.sleep(0.1)
                    continue

                readable, _, _ = select.select(read_list, [], [], 0.1)

                for sock in readable:
                    try:
                        data, addr = sock.recvfrom(65536)
                        tunnel = sockets_map.get(sock)
                        if not tunnel:
                            continue

                        self._handle_packet(tunnel, sock, data, addr)

                    except OSError:
                        pass
                    except Exception as e:
                        LoggerFactory.get_logger().error(f"Receive error: {e}")

            except Exception as e:
                LoggerFactory.get_logger().error(f"Receive loop error: {e}")
                time.sleep(1)

    def _handle_packet(self, tunnel, sock: socket.socket,
                       data: bytes, addr: Tuple[str, int]):
        # 1. 优先处理 N4 信令
        if len(data) == N4Packet.SIZE:
            ident = N4Packet.dec_punch(data)
            if ident is not None:
                self._handle_punch_received(tunnel, sock, addr, ident)
                return

        # 2. 隧道未建立则忽略
        if not tunnel.is_established():
            return

        # 3. KCP 输入处理 (带粘包/拆包修复)
        if hasattr(tunnel, 'kcp'):
            # Collect complete packets while holding the lock
            complete_packets = []

            with tunnel.lock:
                # [Input] 将 UDP 原始数据喂给 KCP
                try:
                    tunnel.kcp.input(data)
                except Exception as e:
                    LoggerFactory.get_logger().warning(f'invalid kcp data: {e}')
                    return

                # [Recv] 从 KCP 获取所有可用的流数据，全部追加到 buffer
                while True:
                    chunk = tunnel.kcp.recv()
                    if not chunk:
                        break
                    tunnel.stream_buffer.extend(chunk)

                # [Parse] 从 buffer 中循环切出完整的 TunnelPacket
                # TunnelPacket 头部固定 8 字节
                HEADER_SIZE = 8

                while len(tunnel.stream_buffer) >= HEADER_SIZE:
                    # 1. 预读取头部，获取包体长度
                    # TunnelPacket格式: type(1) + flags(1) + uid(4) + length(2)
                    # length 是最后两个字节 (大端序 !H)
                    # 这里的索引 6 和 7 对应 length 字段
                    payload_len = (tunnel.stream_buffer[6] << 8) | tunnel.stream_buffer[7]
                    if payload_len > 10 * 1024 * 1024:
                        LoggerFactory.get_logger().error(f"异常包长度 {payload_len}，流已错位，重置缓冲区")
                        tunnel.stream_buffer.clear()
                        break

                    total_pkt_len = HEADER_SIZE + payload_len

                    if len(tunnel.stream_buffer) >= total_pkt_len:
                        complete_packet = bytes(tunnel.stream_buffer[:total_pkt_len])
                        del tunnel.stream_buffer[:total_pkt_len]
                        # Collect packet, process outside lock to avoid blocking
                        complete_packets.append(complete_packet)
                    else:
                        # 数据不够一个完整包，等待下次 KCP 数据到来
                        break

            # Process packets outside the lock to prevent blocking concurrent sends
            for packet in complete_packets:
                self._process_tunnel_payload(tunnel, sock, packet, addr)

    def _process_tunnel_payload(self, tunnel, sock, data, addr):
        """解析 TunnelPacket (原有的业务逻辑)"""
        try:
            result = TunnelPacket.unpack(data)
        except:
            return

        if result is None: return

        pkt_type, flags, uid, payload = result
        tunnel.last_activity = time.time()

        if pkt_type == TunnelPacket.TYPE_DATA:
            # Auto-register UID to this tunnel's peer for bidirectional communication
            # This allows responses to be sent back via the same P2P tunnel
            if uid not in self.uid_to_peer:
                self.register_uid(uid, tunnel.peer_name)
                LoggerFactory.get_logger().debug(
                    f'Auto-registered UID {uid.hex()} to peer {tunnel.peer_name}'
                )
            if self.on_data_received:
                self.on_data_received(uid, payload)

        elif pkt_type == TunnelPacket.TYPE_CLOSE_UID:
            self.unregister_uid(uid)

        elif pkt_type == TunnelPacket.TYPE_KEEPALIVE:
            try:
                # 必须通过 tunnel.send (即 KCP) 回复
                tunnel.send(TunnelPacket.pack_keepalive_ack())
            except:
                pass

        elif pkt_type == TunnelPacket.TYPE_KEEPALIVE_ACK:
            # Received keepalive response, tunnel is healthy
            tunnel.keepalive_pending = False
            tunnel.keepalive_failures = 0  # Reset failure count
            LoggerFactory.get_logger().debug(
                f"Received keepalive ACK from {tunnel.peer_name}"
            )

        elif pkt_type == TunnelPacket.TYPE_TUNNEL_CLOSE:
            self.close_tunnel(tunnel.peer_name, notify_peer=False)


    def _handle_punch_received(self, tunnel: N4Tunnel, sock: socket.socket,
                               addr: Tuple[str, int], ident: bytes):
        """Handle received PUNCH packet during hole punching"""
        if tunnel.status != 'punching':
            return

        # Verify session ID matches
        if ident != tunnel.session_id:
            return

        LoggerFactory.get_logger().info(
            f"PUNCH received from {tunnel.peer_name} at {addr}"
        )

        with tunnel.lock:
            if tunnel.status == 'established':
                return

            # Hole punch successful
            tunnel.status = 'established'
            tunnel.active_socket = sock
            tunnel.peer_addr = addr
            tunnel.last_activity = time.time()

            # Close other sockets
            # for s in tunnel.socket_pool:
                # if s != sock:
                #     try:
                #         s.close()
                #     except:
                #         pass
            # tunnel.socket_pool = [sock]

        # Send PUNCH back to confirm (multiple times for reliability)
        punch_pkt = N4Packet.punch(tunnel.session_id)
        for _ in range(5):
            try:
                sock.sendto(punch_pkt, addr)
            except:
                pass
            time.sleep(0.1)

        LoggerFactory.get_logger().info(
            f"Tunnel ESTABLISHED to {tunnel.peer_name} via {addr}"
        )

    def _monitor_loop(self):
        """Background monitor for tunnel health and idle timeout"""
        while self.running:
            time.sleep(5)  # Check more frequently for better responsiveness

            try:
                now = time.time()

                with self.lock:
                    tunnels_to_check = list(self.tunnels.values())

                for tunnel in tunnels_to_check:
                    # Check punching timeout
                    if tunnel.status == 'punching':
                        punch_elapsed = now - tunnel.punch_start_time
                        if punch_elapsed > self.punch_timeout:
                            LoggerFactory.get_logger().warning(
                                f"Punch timeout for {tunnel.peer_name} after {punch_elapsed:.1f}s"
                            )
                            # Mark as failed, don't close - let it be cleaned up
                            tunnel.status = 'failed'
                            if self.on_punch_failed:
                                self.on_punch_failed(tunnel.peer_name)
                        continue

                    # Clean up failed tunnels after 60s so they can be retried
                    if tunnel.status == 'failed':
                        failed_time = now - tunnel.punch_start_time
                        if failed_time > 60:
                            self.clear_failed_tunnel(tunnel.peer_name)
                        continue

                    if not tunnel.is_established():
                        continue

                    idle_time = now - tunnel.last_activity

                    # Check keepalive response timeout
                    if tunnel.keepalive_pending:
                        keepalive_wait = now - tunnel.last_keepalive_sent
                        if keepalive_wait > 5:  # 10s timeout for keepalive response
                            tunnel.keepalive_failures += 1
                            tunnel.keepalive_pending = False
                            LoggerFactory.get_logger().warning(
                                f"Keepalive timeout for {tunnel.peer_name}, "
                                f"failures: {tunnel.keepalive_failures}/{tunnel.max_keepalive_failures}"
                            )

                            if tunnel.keepalive_failures >= tunnel.max_keepalive_failures:
                                LoggerFactory.get_logger().error(
                                    f"Tunnel to {tunnel.peer_name} is dead (keepalive failed)"
                                )
                                self.close_tunnel(tunnel.peer_name, notify_peer=True)
                                continue

                    # Send keepalive every 15s of idle time
                    if idle_time > 10 and not tunnel.keepalive_pending:
                        tunnel.send_keepalive()
                        tunnel.last_keepalive_sent = now
                        tunnel.keepalive_pending = True
                        LoggerFactory.get_logger().debug(
                            f"Sent keepalive to {tunnel.peer_name}"
                        )

                    # Hard timeout at 60s idle (no activity at all)
                    if idle_time > 60:
                        LoggerFactory.get_logger().info(
                            f"Tunnel to {tunnel.peer_name} idle timeout"
                        )
                        self.close_tunnel(tunnel.peer_name)

            except Exception as e:
                LoggerFactory.get_logger().error(f"Monitor loop error: {e}")
