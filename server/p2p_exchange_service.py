"""
UDP Exchange Service for P2P Hole Punching
Listens for UDP packets from clients to establish NAT mapping
and records their actual public UDP ports.
"""
import socket
import threading
import time
import traceback
from typing import Dict, Optional, Tuple

from common.logger_factory import LoggerFactory


class P2PExchangeService:
    """
    UDP服务，用于接收客户端的EXCHANGE包并记录其实际UDP外网端口
    """
    _instance = None

    def __init__(self, port: int):
        self.port = port
        self.running = False
        self.sock: Optional[socket.socket] = None
        self.thread: Optional[threading.Thread] = None

        # client_name -> {'ip': str, 'port': int, 'timestamp': float}
        self.client_udp_info: Dict[str, Dict] = {}
        self.lock = threading.Lock()

        # Cleanup interval (seconds)
        self.cleanup_interval = 60
        self.max_age = 300  # 5 minutes

        # Tornado IOLoop reference (for thread-safe callback)
        self.tornado_loop = None

    @classmethod
    def get_instance(cls, port: int = 19999):
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls(port)
        return cls._instance

    def set_tornado_loop(self, loop):
        """设置Tornado IOLoop引用（必须在启动前调用）"""
        self.tornado_loop = loop

    def start(self):
        """启动UDP监听服务"""
        if self.running:
            LoggerFactory.get_logger().warning('P2P Exchange Service already running')
            return

        # 如果没有设置tornado_loop，尝试获取当前的
        if not self.tornado_loop:
            try:
                import tornado.ioloop
                self.tornado_loop = tornado.ioloop.IOLoop.current()
            except:
                LoggerFactory.get_logger().warning('Failed to get Tornado IOLoop, P2P PEER_INFO may not work')

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                if hasattr(socket, "SO_REUSEPORT"):
                    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except:
                pass

            self.sock.bind(('0.0.0.0', self.port))
            self.running = True

            # 启动接收线程
            self.thread = threading.Thread(target=self._receive_loop, daemon=True)
            self.thread.start()

            # 启动清理线程
            cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
            cleanup_thread.start()

            LoggerFactory.get_logger().info(f'P2P Exchange Service started on UDP port {self.port}')
        except Exception as e:
            LoggerFactory.get_logger().error(f'Failed to start P2P Exchange Service: {e}')
            LoggerFactory.get_logger().error(traceback.format_exc())
            raise

    def stop(self):
        """停止UDP监听服务"""
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        LoggerFactory.get_logger().info('P2P Exchange Service stopped')

    def _receive_loop(self):
        """接收UDP包的主循环"""
        LoggerFactory.get_logger().info('P2P Exchange receive loop started')

        while self.running:
            try:
                # 设置超时避免阻塞
                self.sock.settimeout(1.0)

                try:
                    data, addr = self.sock.recvfrom(1024)
                except socket.timeout:
                    continue

                # 解析数据包
                # 支持两种协议：
                # 1. EXCHANGE: b'EXCHANGE:' + client_name (新协议，用于P2P打洞)
                # 2. P2P_PING: b'P2P_PING:' + client_name (旧协议，用于心跳)

                if data.startswith(b'EXCHANGE:'):
                    # 新协议：EXCHANGE包
                    try:
                        client_name = data[9:].decode('utf-8').strip()
                    except:
                        LoggerFactory.get_logger().warning(f'Failed to decode client name from {addr}')
                        continue

                    if not client_name:
                        continue

                    # 记录客户端的实际UDP外网地址
                    with self.lock:
                        old_info = self.client_udp_info.get(client_name)
                        self.client_udp_info[client_name] = {
                            'ip': addr[0],
                            'port': addr[1],  # 这是NAT分配的实际UDP端口！
                            'timestamp': time.time()
                        }

                    # 总是记录EXCHANGE日志（重要！）
                    LoggerFactory.get_logger().info(
                        f'Received EXCHANGE from {client_name}: {addr[0]}:{addr[1]}'
                    )

                    # 发送ACK回复
                    try:
                        ack_packet = b'EXCHANGE_ACK'
                        self.sock.sendto(ack_packet, addr)
                    except:
                        pass

                    # 触发peer info通知检查
                    self._check_and_notify_peers(client_name)

                elif data.startswith(b'P2P_PING:'):
                    # 旧协议：心跳包（兼容性支持）
                    try:
                        client_name = data.split(b':')[1].decode('utf-8')

                        # Import here to avoid circular dependency
                        from server.websocket_handler import MyWebSocketaHandler

                        # Find handler by name
                        handler = MyWebSocketaHandler.client_name_to_handler.get(client_name)

                        if handler:
                            # Update public address (only log if changed)
                            if handler.public_port != addr[1] or handler.public_ip != addr[0]:
                                LoggerFactory.get_logger().info(f"P2P Addr Update [{client_name}]: {addr[0]}:{addr[1]}")
                            handler.public_ip = addr[0]
                            handler.public_port = addr[1]
                    except Exception as e:
                        pass  # Silently ignore malformed P2P_PING

                else:
                    # Unknown protocol, ignore
                    # LoggerFactory.get_logger().debug(f'Unknown packet from {addr}: {data[:20]}')
                    continue

            except Exception as e:
                if self.running:
                    LoggerFactory.get_logger().error(f'Error in exchange receive loop: {e}')
                    LoggerFactory.get_logger().error(traceback.format_exc())
                time.sleep(0.1)

    def _cleanup_loop(self):
        """定期清理过期的UDP信息"""
        while self.running:
            time.sleep(self.cleanup_interval)

            try:
                now = time.time()
                with self.lock:
                    expired = [
                        client for client, info in self.client_udp_info.items()
                        if now - info['timestamp'] > self.max_age
                    ]

                    for client in expired:
                        del self.client_udp_info[client]
                        LoggerFactory.get_logger().debug(f'Cleaned up expired UDP info for {client}')

            except Exception as e:
                LoggerFactory.get_logger().error(f'Error in cleanup loop: {e}')

    def _check_and_notify_peers(self, client_name: str):
        """检查是否有等待此客户端的peer，如果双方都ready则发送PEER_INFO"""
        try:
            # Import here to avoid circular dependency
            from server.websocket_handler import MyWebSocketaHandler
            from constant.message_type_constnat import MessageTypeConstant
            from common.nat_serialization import NatSerialization
            from context.context_utils import ContextUtils

            handler = MyWebSocketaHandler.client_name_to_handler.get(client_name)
            if not handler:
                LoggerFactory.get_logger().warning(f'Handler not found for {client_name}')
                return

            # 更新handler的udp_public_port
            with self.lock:
                info = self.client_udp_info.get(client_name)
                if info:
                    handler.udp_public_port = info['port']
                    handler.public_ip = info['ip']
                    LoggerFactory.get_logger().debug(f'Updated {client_name} UDP port: {info["port"]}')

            # 检查所有等待的peer pair
            # 这里需要websocket_handler维护一个pending_p2p_pairs字典
            # 格式: (client_a, client_b) -> [uid1, uid2, ...]
            if not hasattr(MyWebSocketaHandler, 'pending_p2p_pairs'):
                LoggerFactory.get_logger().warning('pending_p2p_pairs not found in MyWebSocketaHandler')
                return

            LoggerFactory.get_logger().debug(f'Checking pending pairs: {list(MyWebSocketaHandler.pending_p2p_pairs.keys())}')

            pairs_to_notify = []
            for pair_key, uids in list(MyWebSocketaHandler.pending_p2p_pairs.items()):
                client_a, client_b = pair_key
                if client_name in (client_a, client_b):
                    # 检查双方是否都有UDP信息
                    handler_a = MyWebSocketaHandler.client_name_to_handler.get(client_a)
                    handler_b = MyWebSocketaHandler.client_name_to_handler.get(client_b)

                    LoggerFactory.get_logger().debug(
                        f'Checking pair ({client_a}, {client_b}): '
                        f'handler_a={handler_a is not None}, handler_b={handler_b is not None}, '
                        f'udp_a={handler_a.udp_public_port if handler_a else None}, '
                        f'udp_b={handler_b.udp_public_port if handler_b else None}'
                    )

                    if (handler_a and handler_b and
                        handler_a.udp_public_port and handler_b.udp_public_port):
                        pairs_to_notify.append((pair_key, uids, handler_a, handler_b))
                        LoggerFactory.get_logger().info(
                            f'Pair ready to notify: {client_a}:{handler_a.udp_public_port} <-> {client_b}:{handler_b.udp_public_port}'
                        )

            # 发送PEER_INFO给所有ready的pairs
            for pair_key, uids, handler_a, handler_b in pairs_to_notify:
                for uid in uids:
                    self._send_peer_info(uid, handler_a, handler_b)

                # 移除已处理的pair
                del MyWebSocketaHandler.pending_p2p_pairs[pair_key]
                LoggerFactory.get_logger().info(f'P2P pair ready: {pair_key[0]} <-> {pair_key[1]}')

        except Exception as e:
            LoggerFactory.get_logger().error(f'Error in check_and_notify_peers: {e}')
            LoggerFactory.get_logger().error(traceback.format_exc())

    def _send_peer_info(self, uid_hex: str, handler_a, handler_b):
        """发送PEER_INFO给双方客户端（线程安全）"""
        try:
            from functools import partial
            from constant.message_type_constnat import MessageTypeConstant
            from common.nat_serialization import NatSerialization
            from context.context_utils import ContextUtils

            if not self.tornado_loop:
                LoggerFactory.get_logger().error('Tornado IOLoop not set, cannot send peer info')
                return

            # 发送给client_a（告知client_b的UDP信息）
            peer_info_to_a = {
                'type_': MessageTypeConstant.P2P_PEER_INFO,
                'data': {
                    'uid': uid_hex,
                    'peer_client': handler_b.client_name,
                    'peer_ip': handler_b.public_ip,
                    'peer_udp_port': handler_b.udp_public_port
                }
            }

            # 发送给client_b（告知client_a的UDP信息）
            peer_info_to_b = {
                'type_': MessageTypeConstant.P2P_PEER_INFO,
                'data': {
                    'uid': uid_hex,
                    'peer_client': handler_a.client_name,
                    'peer_ip': handler_a.public_ip,
                    'peer_udp_port': handler_a.udp_public_port
                }
            }

            # 序列化消息
            msg_to_a = NatSerialization.dumps(peer_info_to_a, ContextUtils.get_password(), handler_a.compress_support)
            msg_to_b = NatSerialization.dumps(peer_info_to_b, ContextUtils.get_password(), handler_b.compress_support)

            # 使用tornado的线程安全方式发送
            # add_callback 是线程安全的，使用 partial 绑定参数
            self.tornado_loop.add_callback(
                partial(handler_a.write_message, msg_to_a, True)
            )

            self.tornado_loop.add_callback(
                partial(handler_b.write_message, msg_to_b, True)
            )

            LoggerFactory.get_logger().info(
                f'Sent P2P_PEER_INFO for UID {uid_hex}: {handler_a.client_name} <-> {handler_b.client_name}'
            )

        except Exception as e:
            LoggerFactory.get_logger().error(f'Error sending peer info: {e}')
            LoggerFactory.get_logger().error(traceback.format_exc())

    def get_client_udp_info(self, client_name: str) -> Optional[Tuple[str, int]]:
        """
        获取客户端的UDP地址信息

        Returns:
            (ip, port) 或 None
        """
        with self.lock:
            info = self.client_udp_info.get(client_name)
            if info:
                return (info['ip'], info['port'])
            return None

    def has_client_info(self, client_name: str) -> bool:
        """检查是否有客户端的UDP信息"""
        with self.lock:
            return client_name in self.client_udp_info

    def get_all_clients(self) -> Dict[str, Dict]:
        """获取所有客户端的UDP信息（用于调试）"""
        with self.lock:
            return dict(self.client_udp_info)
