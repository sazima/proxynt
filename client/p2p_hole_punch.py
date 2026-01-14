"""
UDP Hole Punching Module for P2P Connection
Features:
1. Multiplexing (Tunnel Reuse)
2. N4 Strategy (Port Prediction + Socket Rotation)
3. Sequential Port Binding
"""
import socket
import select
import threading
import time
import traceback
from typing import Dict, Optional, Callable, List

from common.logger_factory import LoggerFactory
from constant.system_constant import SystemConstant


class P2PTunnel:
    """Represents a persistent P2P tunnel to a specific Peer"""
    def __init__(self, peer_name: str, remote_ip: str, remote_base_port: int):
        self.peer_name = peer_name
        self.remote_ip = remote_ip
        self.remote_base_port = remote_base_port

        # Sockets pool for punching
        self.sockets: List[socket.socket] = []

        # Active channel (The winner socket)
        self.active_socket: Optional[socket.socket] = None
        self.active_remote_addr: Optional[tuple] = None

        self.established = False
        self.last_active_time = time.time()
        self.status = 'connecting' # connecting, established


class P2PHolePunch:
    """UDP Hole Punching Manager"""

    def __init__(self):
        # Key: peer_client_name (String), Value: P2PTunnel
        self.tunnels: Dict[str, P2PTunnel] = {}
        # Key: uid (bytes), Value: peer_client_name (String)
        self.uid_to_peer: Dict[bytes, str] = {}

        self.lock = threading.Lock()
        self.running = False
        self.receive_thread: Optional[threading.Thread] = None

        self.ws_client_ref = None # Reference to WebsocketClient for reconnection signals

        # --- N4 Strategy Configuration ---
        self.socket_batch_size = 25  # Number of sockets per batch
        self.port_range = 20         # Prediction range (+/-) - 减小范围，n4使用20
        self.rotation_interval = 20   # Rotate sockets every N rounds - 加快轮换
        self.max_total_rounds = 60   # Give up after N rounds
        self.current_bind_port = 30000 # Start binding from this port (Sequential)

        # Callbacks
        self.on_data_received: Optional[Callable[[bytes, bytes], None]] = None

    def set_ws_client(self, ws_client):
        self.ws_client_ref = ws_client
        # Start monitor thread
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    def start(self):
        if self.running:
            return
        self.running = True
        self.receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.receive_thread.start()
        LoggerFactory.get_logger().info(f'P2P Service Started. Strategy: {self.socket_batch_size} sockets, Range +/-{self.port_range}')

    def is_tunnel_active(self, peer_name: str) -> bool:
        with self.lock:
            tunnel = self.tunnels.get(peer_name)
            if tunnel and tunnel.established:
                return True
        return False

    def stop(self):
        self.running = False
        with self.lock:
            for peer, tunnel in list(self.tunnels.items()):
                self._close_tunnel_sockets(tunnel)
            self.tunnels.clear()
            self.uid_to_peer.clear()
        LoggerFactory.get_logger().info('P2P Service Stopped')

    def _close_tunnel_sockets(self, tunnel: P2PTunnel):
        """Close all sockets associated with a tunnel"""
        if tunnel.sockets:
            for s in tunnel.sockets:
                try:
                    s.close()
                except:
                    pass
            tunnel.sockets.clear()

        # Note: We do NOT close active_socket here if it is established,
        # unless we are destroying the tunnel completely.

    def _create_sockets_batch(self, tunnel: P2PTunnel):
        """Create a new batch of sockets with SEQUENTIAL ports"""
        # Close old non-active sockets first to release ports
        self._close_tunnel_sockets(tunnel)

        count = 0
        while count < self.socket_batch_size:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    if hasattr(socket, "SO_REUSEPORT"):
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except:
                    pass

                # Bind to sequential port
                sock.bind(('0.0.0.0', self.current_bind_port))
                sock.setblocking(False)
                tunnel.sockets.append(sock)
                count += 1
            except OSError:
                # Port busy, try next
                pass
            finally:
                self.current_bind_port += 1
                if self.current_bind_port > 60000:
                    self.current_bind_port = 30000

        start_port = self.current_bind_port - count
        if start_port < 30000: start_port += 30000 # rough fix for log display
        LoggerFactory.get_logger().info(f"Generated batch of {len(tunnel.sockets)} sockets for {tunnel.peer_name} (Base Port ~{start_port})")

    def register_session(self, uid: bytes, peer_name: str):
        """Register a UID to a Peer (Call this when creating TCP connection)"""
        with self.lock:
            self.uid_to_peer[uid] = peer_name

    def prepare_for_exchange(self, peer_name: str) -> Optional[socket.socket]:
        """
        Prepare for EXCHANGE phase by creating initial punching sockets.
        Returns the first socket to use for sending EXCHANGE packet.

        This ensures EXCHANGE is sent from an actual punching socket,
        not a temporary socket (critical for NAT mapping consistency).
        """
        with self.lock:
            tunnel = self.tunnels.get(peer_name)

            # If tunnel already exists with sockets, return first one
            if tunnel and tunnel.sockets:
                return tunnel.sockets[0]

            # Create new tunnel with initial sockets
            tunnel = P2PTunnel(peer_name, "0.0.0.0", 0)  # Dummy IP/port for now
            self.tunnels[peer_name] = tunnel
            self._create_sockets_batch(tunnel)

            if tunnel.sockets:
                LoggerFactory.get_logger().info(f'Prepared {len(tunnel.sockets)} sockets for EXCHANGE with {peer_name}')
                return tunnel.sockets[0]

            return None

    def initiate_connection(self, uid: bytes, peer_name: str, remote_ip: str, remote_port: int) -> bool:
        """
        Check if tunnel exists. If not, start punching.
        """
        self.register_session(uid, peer_name)

        with self.lock:
            tunnel = self.tunnels.get(peer_name)

            # Scenario 1: Tunnel exists
            if tunnel:
                # Check if this is a pre-created tunnel from EXCHANGE (has dummy IP)
                is_precreated = (tunnel.remote_ip == "0.0.0.0" and tunnel.remote_base_port == 0)

                if is_precreated:
                    LoggerFactory.get_logger().info(f"Updating pre-created tunnel for {peer_name} with actual peer info")
                    tunnel.remote_ip = remote_ip
                    tunnel.remote_base_port = remote_port
                    threading.Thread(target=self._punch_loop, args=(tunnel, uid), daemon=True).start()
                    return True

                # Network change - remote info changed (e.g. Peer Restarted)
                if tunnel.remote_ip != remote_ip or tunnel.remote_base_port != remote_port:
                    LoggerFactory.get_logger().info(f"Peer {peer_name} address changed. Re-punching using EXISTING socket.")

                    # === [核心修改 START] ===
                    # 不要关闭 Socket！保留 active_socket 以维持公网端口映射 (NAT Mapping)
                    # 因为服务器告诉对方的依然是我们当前的公网端口

                    # 1. 标记为未建立，触发 punch_loop 发送数据
                    tunnel.established = False

                    # 2. 确保 active_socket 在 sockets 列表中，这样 _punch_loop 会使用它发送
                    if tunnel.active_socket:
                        if tunnel.active_socket not in tunnel.sockets:
                            tunnel.sockets.insert(0, tunnel.active_socket)

                    # 3. 如果 sockets 为空 (极少见), 才需要创建新的
                    if not tunnel.sockets:
                        self._create_sockets_batch(tunnel)
                    # === [核心修改 END] ===

                    # Update target info
                    tunnel.remote_ip = remote_ip
                    tunnel.remote_base_port = remote_port

                    # Restart punching
                    threading.Thread(target=self._punch_loop, args=(tunnel, uid), daemon=True).start()
                    return True

                if tunnel.established:
                    return True

                return True

            # Scenario 2: New Tunnel
            tunnel = P2PTunnel(peer_name, remote_ip, remote_port)
            self.tunnels[peer_name] = tunnel
            threading.Thread(target=self._punch_loop, args=(tunnel, uid), daemon=True).start()

            return True
    def _punch_loop(self, tunnel: P2PTunnel, initial_uid: bytes):
        """
        The heavy lifting: sending packets to guess the port.
        """
        retry_count = 0
        # Handshake packet. Using initial_uid allows compatibility with legacy logic
        punch_packet = b'PUNCH:' + initial_uid

        # Initial batch (skip if sockets already created during EXCHANGE)
        # 如果是 EXCHANGE 流程，这里 sockets 已经存在且包含关键 NAT 映射，不会重新创建
        if not tunnel.sockets:
            self._create_sockets_batch(tunnel)

        while self.running and not tunnel.established and retry_count < self.max_total_rounds:
            try:
                # 1. Calculate Target Ports (Center + Prediction)
                target_ports = set()
                base = int(tunnel.remote_base_port)
                target_ports.add(base)

                for i in range(1, self.port_range + 1):
                    p1 = base + i
                    p2 = base - i
                    if p1 <= 65535: target_ports.add(p1)
                    if p2 > 0: target_ports.add(p2)

                # 2. Send Packets (Burst)
                # Copy list to be thread-safe during rotation
                current_sockets = list(tunnel.sockets)
                packet_sent = 0

                for sock in current_sockets:
                    for port in target_ports:
                        try:
                            sock.sendto(punch_packet, (tunnel.remote_ip, port))
                            packet_sent += 1
                        except OSError:
                            pass

                if retry_count % 5 == 0:
                    LoggerFactory.get_logger().debug(f'Punching {tunnel.peer_name}: Round {retry_count}/{self.max_total_rounds}, sent {packet_sent} pkts')

                time.sleep(0.5)
                retry_count += 1

                # 3. Socket Rotation
                # [关键修改]: 增加 retry_count > 20 判断。
                # 前 20 轮 (约10秒) 不进行 Socket 轮换。
                # 只有当初始 Socket (包含 EXCHANGE 映射) 尝试 10 秒后仍未打通，才开始轮换尝试预测端口。
                if not tunnel.established and retry_count > 20 and retry_count % self.rotation_interval == 0:
                    LoggerFactory.get_logger().info(f"Rotating sockets for {tunnel.peer_name}...")
                    self._create_sockets_batch(tunnel)

                # Check if established (set by receive loop)
                if tunnel.established:
                    return

            except Exception as e:
                LoggerFactory.get_logger().error(f"Punch loop error: {e}")
                LoggerFactory.get_logger().error(traceback.format_exc())
                break

        if not tunnel.established:
            LoggerFactory.get_logger().warning(f"Hole punching failed for {tunnel.peer_name} after {retry_count} rounds")
            # Cleanup failed tunnel to allow retry later
            with self.lock:
                if self.tunnels.get(tunnel.peer_name) == tunnel:
                    self.tunnels.pop(tunnel.peer_name)
            self._close_tunnel_sockets(tunnel)

    def _receive_loop(self):
        """
        Listen on ALL sockets (both active tunnel sockets and punching candidates)
        """
        while self.running:
            try:
                sockets_map = {} # socket -> tunnel
                read_list = []

                with self.lock:
                    for peer, tunnel in self.tunnels.items():
                        if tunnel.established and tunnel.active_socket:
                            read_list.append(tunnel.active_socket)
                            sockets_map[tunnel.active_socket] = tunnel
                        else:
                            # If punching, listen on all candidate sockets
                            for s in tunnel.sockets:
                                read_list.append(s)
                                sockets_map[s] = tunnel

                if not read_list:
                    time.sleep(0.1)
                    continue

                # Wait for data
                readable, _, _ = select.select(read_list, [], [], 0.1)

                for sock in readable:
                    try:
                        data, addr = sock.recvfrom(65536)
                        tunnel = sockets_map.get(sock)
                        if not tunnel: continue

                        # === Case 1: Handshake (PUNCH / PUNCH_ACK) ===
                        if data.startswith(b'PUNCH'):
                            is_ack = data.startswith(b'PUNCH_ACK:')
                            # Extract UID used for handshake (format: PUNCH:UID)
                            try:
                                uid_part = data.split(b':')[1]
                            except:
                                continue

                            if not tunnel.established:
                                LoggerFactory.get_logger().info(f"WINNER! Tunnel Established to {tunnel.peer_name} via {addr}")
                                tunnel.established = True
                                tunnel.active_socket = sock
                                tunnel.active_remote_addr = addr

                                # Close other useless sockets
                                for s in tunnel.sockets:
                                    if s != sock:
                                        try: s.close()
                                        except: pass
                                tunnel.sockets = [sock]

                            # Always reply ACK to PUNCH to ensure both sides know
                            if not is_ack:
                                try:
                                    sock.sendto(b'PUNCH_ACK:' + uid_part, addr)
                                except: pass

                            continue

                        # === Case 2: Data Transfer (Multiplexed) ===
                        # Packet Format: b'DATA:' + UID(4 bytes) + Content
                        if data.startswith(b'DATA:') and len(data) >= 9:
                            uid = data[5:9]
                            payload = data[9:]

                            tunnel.last_active_time = time.time()

                            # Dispatch to upper layer (tcp_forward_client)
                            if self.on_data_received:
                                self.on_data_received(uid, payload)

                    except OSError:
                        pass # Socket errors are common
                    except Exception as e:
                        LoggerFactory.get_logger().error(f"Receive processing error: {e}")

            except Exception:
                time.sleep(1)

    def send_data(self, uid: bytes, data: bytes) -> bool:
        """Send data via tunnel. Wraps UID into packet."""
        peer_name = self.uid_to_peer.get(uid)
        if not peer_name:
            # LoggerFactory.get_logger().debug(f"UID {uid.hex()} has no peer mapping")
            return False

        tunnel = self.tunnels.get(peer_name)
        if not tunnel or not tunnel.established:
            return False

        # Wrap Packet: DATA: + UID + Payload
        packet = b'DATA:' + uid + data
        try:
            tunnel.active_socket.sendto(packet, tunnel.active_remote_addr)
            return True
        except:
            return False

    def is_ready(self, uid: bytes) -> bool:
        """Check if P2P tunnel is ready for this UID"""
        peer_name = self.uid_to_peer.get(uid)
        if not peer_name: return False
        tunnel = self.tunnels.get(peer_name)
        return tunnel is not None and tunnel.established

    def close_session(self, uid: bytes):
        """Remove the UID mapping, but keep the tunnel alive"""
        with self.lock:
            self.uid_to_peer.pop(uid, None)

    def _monitor_loop(self):
        """Background monitor for tunnel health and auto-reconnect"""
        while self.running:
            time.sleep(10)
            try:
                with self.lock:
                    current_tunnels = list(self.tunnels.values())

                now = time.time()
                for tunnel in current_tunnels:
                    # 如果隧道已建立，且超过 10 秒没有发送数据，发送一个空包保活，防止 NAT 映射过期
                    if tunnel.established and tunnel.active_socket:
                        if now - tunnel.last_active_time > 10:
                            try:
                                # 发送一个特殊的 PING 包给对端 (复用通道)
                                # 注意：这里不需要对端回复，只要有包出入，NAT 就会保持开启
                                keep_alive_packet = b'PUNCH:KEEP_ALIVE'
                                tunnel.active_socket.sendto(keep_alive_packet, tunnel.active_remote_addr)
                                # LoggerFactory.get_logger().debug(f"Sent Keep-Alive to {tunnel.peer_name}")
                            except Exception:
                                pass

                    # 2. 超时清理 (针对正在连接中但迟迟不成功的隧道)
                    if not tunnel.established and (now - tunnel.last_active_time > 65):
                        LoggerFactory.get_logger().warn(f"Tunnel handshake to {tunnel.peer_name} timed out.")
                        self._close_tunnel_sockets(tunnel)
                        with self.lock:
                            self.tunnels.pop(tunnel.peer_name, None)

                        # Trigger Reconnect via WebSocket
                        if self.ws_client_ref:
                            self.ws_client_ref.send_p2p_pre_connect(tunnel.peer_name)
            except Exception as e:
                LoggerFactory.get_logger().error(f"Monitor loop error: {e}")