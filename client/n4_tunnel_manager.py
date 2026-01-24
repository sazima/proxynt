import logging
import socket
import threading
import time
from typing import Dict, Optional, Tuple, Type, Set

from common.logger_factory import LoggerFactory
from common.n4_punch import N4PunchClient, N4Error
from .abstract_tunnel import AbstractTunnel
from .kcp_tunnel_impl import KcpTunnelImpl

def get_pair_key(client_a: str, client_b: str) -> Tuple[str, str]:
    """获取两个客户端唯一的配对键"""
    return (min(client_a, client_b), max(client_a, client_b))

class N4TunnelManager:
    """
    P2P 隧道管理器
    负责物理打洞的协调及抽象协议隧道的生命周期管理。
    支持：即时中转连接 -> 后台打洞 -> 成功后无缝切换
    """
    def __init__(self, local_client_name: str, server_host: str, server_port: int,
                 tunnel_class: Type[AbstractTunnel] = KcpTunnelImpl):

        self.local_client_name = local_client_name
        self.server_host = server_host
        self.server_port = server_port

        # 核心：解耦后的协议实现类构造器
        self.TunnelClass = tunnel_class

        # 存储已建立的隧道对象 {peer_name: AbstractTunnel}
        self.tunnels: Dict[str, AbstractTunnel] = {}

        # 正在进行的物理打洞客户端 {pair_key: N4PunchClient}
        self.punch_clients: Dict[Tuple[str, str], N4PunchClient] = {}

        # 映射关系 {uid: peer_name}
        self.uid_to_peer: Dict[bytes, str] = {}

        # 状态控制
        self.punching_peers: Set[str] = set()      # 正在进行打洞握手中的 Peer (防止重复触发)

        # 冷却时间管理
        self.last_punch_attempt: Dict[str, float] = {}
        self.PUNCH_COOLDOWN = 60  # 失败后冷却 60 秒
        self.REQUEST_DEBOUNCE = 5 # 请求去抖动 5 秒

        self.lock = threading.Lock()
        self.logger = LoggerFactory.get_logger()

        # 外部回调（由 run_client.py 注入）
        self.ws_client = None
        self.on_data_received = None # func(uid, data)
        self.on_tunnel_closed = None # func(peer_name)
        self.on_punch_failed = None  # func(peer_name)

        self.running = False
        self.update_thread = None

    def set_ws_client(self, ws):
        self.ws_client = ws

    def start(self):
        """启动管理器：启动全局驱动线程"""
        with self.lock:
            if not self.running:
                self.running = True
                self.update_thread = threading.Thread(target=self._global_update_loop, daemon=True)
                self.update_thread.start()
                self.logger.info(f"N4 Tunnel Manager Started (Protocol: {self.TunnelClass.__name__})")

    def _global_update_loop(self):
        """每 10ms 驱动一次所有活跃协议的状态机"""
        while self.running:
            # 获取当前所有隧道的快照进行遍历
            with self.lock:
                active_tunnels = list(self.tunnels.values())

            for tunnel in active_tunnels:
                try:
                    tunnel.update()
                except Exception as e:
                    self.logger.error(f"Error updating tunnel {tunnel.peer_name}: {e}")

            time.sleep(0.01)

    def send_data(self, uid: bytes, data: bytes, trigger_punch: bool = False) -> bool:
        """
        业务数据发送入口（无缝切换的核心）
        :param uid: 连接ID
        :param data: 数据内容
        :param trigger_punch: 如果隧道不存在，是否允许主动发起打洞请求 (Client A=True, Client B=False)
        :return: True=已走隧道, False=需走中转
        """
        peer_name = self.uid_to_peer.get(uid)
        if not peer_name:
            return False # 未知目标，走中转

        with self.lock:
            tunnel = self.tunnels.get(peer_name)

        # 1. 隧道已就绪，直接发送
        if tunnel:
            if tunnel.send(uid, data):
                return True
            else:
                self.logger.warn(f"P2P Tunnel to {peer_name} send failed, closing and fallback to relay.")
                self._handle_failure(peer_name)
                # 隧道发送失败，返回 False 让上层走中转，并触发下一次打洞重试检查

        # 2. 隧道不可用，如果是发起端(A)，检查是否需要打洞
        if trigger_punch:
            self._trigger_punch_if_needed(peer_name)

        # 3. 返回 False，指示调用者使用 WebSocket 中转（保证即时连接性）
        return False

    def _trigger_punch_if_needed(self, peer_name: str):
        """判断是否需要发起打洞请求"""
        now = time.time()

        with self.lock:
            # 如果正在打洞中（物理 UDP Socket 已经创建并正在交互），跳过
            if peer_name in self.punching_peers:
                return

            # 检查冷却时间
            last_time = self.last_punch_attempt.get(peer_name, 0)

            if now - last_time < self.REQUEST_DEBOUNCE:
                return

            # 更新尝试时间，用于去抖动
            # [注意] 这里不能把 peer_name 加入 self.punching_peers
            # 必须等到 prepare_punch 被调用时才加入，否则 has_tunnel() 会在 PUNCH_REQUEST 回来时误判
            self.last_punch_attempt[peer_name] = now

        # 发起请求（异步，不阻塞当前数据流）
        if self.ws_client:
            self.logger.info(f"[Auto Punch] Requesting P2P tunnel for {peer_name} (Background)...")
            threading.Thread(target=self.ws_client._send_punch_request, args=(peer_name,), daemon=True).start()

    def prepare_punch(self, peer_name: str, session_id: bytes) -> Optional[socket.socket]:
        """收到 PUNCH_REQUEST：初始化 Socket 池并返回用于 EXCHANGE 的第一个 Socket"""
        with self.lock:
            # [关键] 真正收到服务端指令，开始打洞流程，此时标记为 punching
            self.punching_peers.add(peer_name)

            # 如果旧隧道还存在（异常情况），先物理关闭
            if peer_name in self.tunnels:
                self.tunnels[peer_name].close()
                self.tunnels.pop(peer_name, None)

            punch_client = N4PunchClient(
                ident=session_id,
                server_host=self.server_host,
                server_port=self.server_port
            )

            try:
                # 使用纯净 UDP 发送握手包
                exchange_sock = punch_client.send_exchange()
                pair_key = get_pair_key(self.local_client_name, peer_name)
                self.punch_clients[pair_key] = punch_client
                return exchange_sock
            except Exception as e:
                self.logger.error(f"Failed to prepare punch for {peer_name}: {e}")
                self.punching_peers.discard(peer_name)
                return None

    def receive_peer_info(self, peer_name: str, peer_ip: str, peer_port: int):
        """收到 PEER_INFO：后台启动物理打洞并初始化具体协议实现"""
        pair_key = get_pair_key(self.local_client_name, peer_name)
        punch_client = self.punch_clients.get(pair_key)

        if punch_client:
            threading.Thread(
                target=self._run_punch_and_setup,
                args=(punch_client, peer_name, peer_ip, peer_port, pair_key),
                daemon=True
            ).start()
        else:
            self.logger.warning(f"Received PEER_INFO for {peer_name} but no punch client found.")
            with self.lock:
                self.punching_peers.discard(peer_name)

    def _run_punch_and_setup(self, punch_client: N4PunchClient, peer_name: str,
                             peer_ip: str, peer_port: int, pair_key: Tuple[str, str]):
        """后台线程：执行打洞 -> 实例化 TunnelImpl -> 建立握手"""
        try:
            # 1. 物理打洞 (纯净 UDP)
            winner_sock, peer_addr = punch_client.punch(peer_ip, peer_port, wait=10)
            self.logger.info(f"Punch SUCCESS for {peer_name}. Setting up {self.TunnelClass.__name__}...")

            # 2. 确定角色
            is_server = self.local_client_name > peer_name

            # 3. 实例化具体的隧道协议实现
            tunnel = self.TunnelClass(
                peer_name=peer_name,
                sock=winner_sock,
                addr=peer_addr,
                on_data_received=self.on_data_received,
                on_established=self._internal_on_established,
                on_closed=self._internal_on_closed,
                is_server=is_server
            )

            # 4. 开始协议激活 (此时 socket 会被升级 QoS)
            tunnel.establish()

        except Exception as e:
            self.logger.warn(f"P2P setup failed for {peer_name}: {e}")
            self._handle_failure(peer_name)

            # 通知上层打洞失败
            if self.on_punch_failed:
                self.on_punch_failed(peer_name)
        finally:
            with self.lock:
                self.punch_clients.pop(pair_key, None)

    def _internal_on_established(self, tunnel: AbstractTunnel):
        """由具体 TunnelImpl 在协议握手成功后调用"""
        peer_name = tunnel.peer_name
        with self.lock:
            self.tunnels[peer_name] = tunnel
            self.punching_peers.discard(peer_name)
            # 成功建立连接，清除冷却时间
            self.last_punch_attempt.pop(peer_name, None)

        self.logger.info(f"=== [P2P READY] {self.TunnelClass.__name__} Tunnel Established for {peer_name} ===")

    def _internal_on_closed(self, peer_name: str):
        """由具体 TunnelImpl 在连接断开时调用"""
        self.logger.info(f"Tunnel closed for {peer_name}, switching to relay.")
        self._handle_failure(peer_name)
        if self.on_tunnel_closed:
            self.on_tunnel_closed(peer_name)

    def _handle_failure(self, peer_name: str):
        """统一失败处理逻辑：清理资源并设置冷却时间"""
        with self.lock:
            self.tunnels.pop(peer_name, None)
            self.punching_peers.discard(peer_name)

            # 设置冷却时间
            self.last_punch_attempt[peer_name] = time.time() - self.REQUEST_DEBOUNCE + self.PUNCH_COOLDOWN

    def register_uid(self, uid: bytes, peer_name: str):
        with self.lock:
            self.uid_to_peer[uid] = peer_name

    def unregister_uid(self, uid: bytes):
        with self.lock:
            self.uid_to_peer.pop(uid, None)

    def is_tunnel_established(self, peer_name: str) -> bool:
        return peer_name in self.tunnels

    def has_tunnel(self, peer_name: str) -> bool:
        """
        判断是否“有隧道”：包含已建立的隧道，或者正在建立中的隧道。
        """
        return peer_name in self.tunnels or peer_name in self.punching_peers

    def get_tunnel(self, peer_name: str) -> Optional[AbstractTunnel]:
        return self.tunnels.get(peer_name)

    def stop(self):
        """停止管理器及所有隧道"""
        self.running = False
        with self.lock:
            for tunnel in list(self.tunnels.values()):
                tunnel.close()
            for puncher in list(self.punch_clients.values()):
                puncher.stop()
            self.tunnels.clear()
            self.punch_clients.clear()