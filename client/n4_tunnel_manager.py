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
        self.kcp.setmtu(1350)

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

    def send_data(self, uid: bytes, data: bytes) -> bool:
        """
        Send data via tunnel.
        Returns False if tunnel not ready.
        """
        peer_name = self.uid_to_peer.get(uid)
        if not peer_name:
            return False

        tunnel = self.get_tunnel(peer_name)
        if not tunnel:
            return False

        # --- 修改回滚 ---
        # 不需要手动 fragment_data，因为 KCP 会处理 MTU 分片
        # 直接封装整个数据包交给 KCP 即可
        packet = TunnelPacket.pack_data(uid, data)

        return tunnel.send(packet)

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
            with tunnel.lock:
                # [Input] 将 UDP 原始数据喂给 KCP
                tunnel.kcp.input(data)

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

                    total_pkt_len = HEADER_SIZE + payload_len

                    # 2. 检查 buffer 里是否有足够的数据包
                    if len(tunnel.stream_buffer) >= total_pkt_len:
                        # 3. 切割出一个完整的包
                        complete_packet = bytes(tunnel.stream_buffer[:total_pkt_len])

                        # 4. 从 buffer 中移除这个包
                        del tunnel.stream_buffer[:total_pkt_len]

                        # 5. 处理这个完整的包
                        self._process_tunnel_payload(tunnel, sock, complete_packet, addr)
                    else:
                        # 数据不够一个完整包，等待下次 KCP 数据到来
                        break

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
            time.sleep(10)

            try:
                now = time.time()

                with self.lock:
                    tunnels_to_check = list(self.tunnels.values())

                for tunnel in tunnels_to_check:
                    if not tunnel.is_established():
                        continue

                    idle_time = now - tunnel.last_activity

                    # Send keepalive at 30s
                    if 30 < idle_time < 35:
                        tunnel.send_keepalive()
                        LoggerFactory.get_logger().debug(
                            f"Sent keepalive to {tunnel.peer_name}"
                        )

                    # Close at 60s idle
                    if idle_time > 60:
                        LoggerFactory.get_logger().info(
                            f"Tunnel to {tunnel.peer_name} idle timeout"
                        )
                        self.close_tunnel(tunnel.peer_name)

            except Exception as e:
                LoggerFactory.get_logger().error(f"Monitor loop error: {e}")
