import asyncio
import logging
import socket
import time
import traceback
import os
from typing import Dict, Set, List, Tuple
from functools import partial
import threading
from asyncio import Lock as AsyncioLock

import tornado

from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.speed_limit import SpeedLimiter
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity

class UdpEndpoint:
    """UDP端点，维护客户端地址到服务端地址的映射"""
    def __init__(self, uid: bytes, address: Tuple[str, int], server):
        self.uid = uid
        self.address = address
        self.server = server
        self.last_active_time = time.time()

class UdpPublicSocketServer:
    """在公网上监听的UDP端口"""
    def __init__(self, s: socket.socket, name: str, ip_port: str, websocket_handler, speed_limit_size: float):
        self.socket_server = s
        self.name = name
        self.ip_port = ip_port
        self.websocket_handler = websocket_handler
        self.speed_limit_size = speed_limit_size
        self.speed_limiter = SpeedLimiter(speed_limit_size) if speed_limit_size else None
        # UDP需要记住每个客户端的地址
        self.addr_to_uid: Dict[str, bytes] = {}
        self.uid_to_endpoint: Dict[bytes, UdpEndpoint] = {}
        self.lock = threading.Lock()

    def add_endpoint(self, address: Tuple[str, int]) -> bytes:
        """添加新的UDP端点，返回生成的UID"""
        addr_key = f"{address[0]}:{address[1]}"
        with self.lock:
            if addr_key in self.addr_to_uid:
                uid = self.addr_to_uid[addr_key]
                self.uid_to_endpoint[uid].last_active_time = time.time()
                return uid

            uid = os.urandom(4)
            endpoint = UdpEndpoint(uid, address, self)
            self.uid_to_endpoint[uid] = endpoint
            self.addr_to_uid[addr_key] = uid
            return uid

    def get_endpoint_by_uid(self, uid: bytes) -> UdpEndpoint:
        """通过UID获取UDP端点"""
        with self.lock:
            return self.uid_to_endpoint.get(uid)

    def remove_endpoint(self, uid: bytes):
        """移除UDP端点"""
        with self.lock:
            if uid in self.uid_to_endpoint:
                endpoint = self.uid_to_endpoint[uid]
                addr_key = f"{endpoint.address[0]}:{endpoint.address[1]}"
                if addr_key in self.addr_to_uid:
                    del self.addr_to_uid[addr_key]
                del self.uid_to_endpoint[uid]

    def clean_inactive_endpoints(self, max_inactive_time: int = 300):
        """清理不活跃的端点"""
        current_time = time.time()
        with self.lock:
            inactive_uids = []
            for uid, endpoint in self.uid_to_endpoint.items():
                if current_time - endpoint.last_active_time > max_inactive_time:
                    inactive_uids.append(uid)

            for uid in inactive_uids:
                self.remove_endpoint(uid)

class UdpForwardClient:
    _instance = None

    def __init__(self, loop=None, tornado_loop=None):
        self.loop = loop or asyncio.get_event_loop()
        self.tornado_loop = tornado_loop or tornado.ioloop.IOLoop.current()
        self.udp_servers: Dict[int, UdpPublicSocketServer] = {}  # 端口号到服务器实例的映射
        self.client_name_to_udp_server_set: Dict[str, Set[UdpPublicSocketServer]] = {}
        self.client_name_to_lock: Dict[str, AsyncioLock] = {}
        self.running = True
        # 启动UDP接收线程
        self.receive_thread = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls(asyncio.get_event_loop(), tornado.ioloop.IOLoop.current())
            cls._instance.start_receive_thread()
        return cls._instance

    def start_receive_thread(self):
        """启动UDP数据接收线程"""
        if self.receive_thread is None:
            self.receive_thread = threading.Thread(target=self._udp_receive_loop)
            self.receive_thread.daemon = True
            self.receive_thread.start()

    def _udp_receive_loop(self):
        """UDP数据接收循环"""
        LoggerFactory.get_logger().info("UDP接收线程已启动")
        while self.running:
            # 遍历所有UDP服务器
            for port, server in list(self.udp_servers.items()):
                try:
                    # 在非阻塞模式下接收数据，避免阻塞其他服务器
                    server.socket_server.setblocking(False)
                    try:
                        data, address = server.socket_server.recvfrom(65536)
                        if data:
                            # 处理收到的UDP数据
                            self._handle_udp_data(server, data, address)
                    except (BlockingIOError, socket.error):
                        # 没有可用数据，继续下一个服务器
                        pass
                except Exception as e:
                    LoggerFactory.get_logger().error(f"UDP数据接收错误: {e}")
                    LoggerFactory.get_logger().error(traceback.format_exc())

            # 防止CPU占用过高
            time.sleep(0.001)

    def _handle_udp_data(self, server: UdpPublicSocketServer, data: bytes, address: Tuple[str, int]):
        """处理收到的UDP数据"""
        # 添加或获取对应的端点
        LoggerFactory.get_logger().info(f"UDP客户端收到来自 {server.socket_server} 的数据: {len(data)} 字节")

        uid = server.add_endpoint(address)

        # 限速处理
        if server.speed_limiter and server.speed_limiter.is_exceed()[0]:
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug('UDP超出速度限制')
            return

        if server.speed_limiter:
            server.speed_limiter.add(len(data))

        # 构造UDP消息并通过WebSocket发送到内网客户端
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_UDP,
            'data': {
                'name': server.name,
                'data': data,
                'uid': uid,
                'ip_port': server.ip_port
            }
        }

        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug(f'发送UDP数据到WebSocket，uid: {uid}, 长度: {len(data)}')

        # 在异步循环中执行WebSocket发送
        is_compress = server.websocket_handler.compress_support
        self.tornado_loop.add_callback(
            partial(server.websocket_handler.write_message, NatSerialization.dumps(send_message, ContextUtils.get_password(), is_compress)), True)

    async def register_udp_server(self, port: int, name: str, ip_port: str, websocket_handler, speed_limit_size: float):
        """注册UDP服务器"""
        client_name = websocket_handler.client_name
        if client_name not in self.client_name_to_lock:
            self.client_name_to_lock[client_name] = AsyncioLock()

        async with self.client_name_to_lock.get(client_name):
            if port in self.udp_servers:
                LoggerFactory.get_logger().warning(f"UDP端口 {port} 已被使用")
                return False

            try:
                # 创建UDP套接字
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('', port))

                # 创建并注册UDP服务器
                server = UdpPublicSocketServer(sock, name, ip_port, websocket_handler, speed_limit_size)
                self.udp_servers[port] = server

                if client_name not in self.client_name_to_udp_server_set:
                    self.client_name_to_udp_server_set[client_name] = set()

                self.client_name_to_udp_server_set[client_name].add(server)
                LoggerFactory.get_logger().info(f"UDP服务器已注册，端口: {port}, 名称: {name}")
                return True
            except Exception as e:
                LoggerFactory.get_logger().error(f"注册UDP服务器失败: {e}")
                LoggerFactory.get_logger().error(traceback.format_exc())
                return False

    async def send_udp(self, uid: bytes, data: bytes, port: int):
        """向UDP客户端发送数据"""
        if port not in self.udp_servers:
            LoggerFactory.get_logger().warning(f"未找到端口为 {port} 的UDP服务器")
            return False

        server = self.udp_servers[port]
        endpoint = server.get_endpoint_by_uid(uid)

        if not endpoint:
            LoggerFactory.get_logger().warning(f"未找到UID为 {uid} 的UDP端点")
            return False

        try:
            # 发送UDP数据
            bytes_sent = server.socket_server.sendto(data, endpoint.address)
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f"UDP数据发送成功，目标: {endpoint.address}, 长度: {bytes_sent}")
            return True
        except Exception as e:
            LoggerFactory.get_logger().error(f"UDP数据发送失败: {e}")
            return False

    async def close_by_client_name(self, client_name: str):
        """关闭指定客户端名称的所有UDP服务器"""
        if client_name not in self.client_name_to_udp_server_set:
            return

        if client_name not in self.client_name_to_lock:
            self.client_name_to_lock[client_name] = AsyncioLock()

        async with self.client_name_to_lock.get(client_name):
            for server in list(self.client_name_to_udp_server_set[client_name]):
                try:
                    # 找到端口并移除
                    for port, srv in list(self.udp_servers.items()):
                        if srv == server:
                            del self.udp_servers[port]
                            break

                    # 关闭套接字
                    server.socket_server.close()
                except Exception as e:
                    LoggerFactory.get_logger().error(f"关闭UDP服务器失败: {e}")

            # 清理集合
            self.client_name_to_udp_server_set.pop(client_name, None)

        # 清理锁
        self.client_name_to_lock.pop(client_name, None)

    def stop(self):
        """停止UDP转发客户端"""
        self.running = False
        for port, server in list(self.udp_servers.items()):
            try:
                server.socket_server.close()
            except Exception:
                pass
        self.udp_servers.clear()
        self.client_name_to_udp_server_set.clear()