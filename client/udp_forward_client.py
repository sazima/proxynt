import asyncio
import logging
import socket
import time
import traceback
import threading
from typing import Dict, Set
from threading import Thread

from common import websocket
from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.speed_limit import SpeedLimiter
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class UdpSocketConnection:
    """连接内网UDP端口的客户端"""
    def __init__(self, uid: bytes, socket_obj: socket.socket, name: str, ip_port: str, ws, speed_limiter=None):
        self.uid = uid
        self.socket = socket_obj
        self.name = name
        self.ip_port = ip_port
        self.ws = ws
        self.speed_limiter = speed_limiter
        self.target_address = None
        self.last_active_time = time.time()

        # 解析目标IP和端口
        if ":" in ip_port:
            ip, port_str = ip_port.split(":")
            self.target_address = (ip, int(port_str))
        else:
            self.target_address = None

class UdpForwardClient:
    """UDP转发客户端"""
    def __init__(self, ws, compress_support: bool):
        self.uid_to_connection: Dict[bytes, UdpSocketConnection] = {}
        self.ws = ws
        self.compress_support = compress_support
        self.running = True
        self.lock = threading.Lock()

        # 启动UDP接收线程
        self.receive_thread = None

    def set_running(self, running: bool):
        """设置是否运行"""
        self.running = running
        if running and not self.receive_thread:
            self.start_receive_thread()

    def start_receive_thread(self):
        """启动UDP接收线程"""
        self.receive_thread = Thread(target=self._udp_receive_loop)
        self.receive_thread.daemon = True
        self.receive_thread.start()

    def _udp_receive_loop(self):
        """UDP数据接收循环"""
        LoggerFactory.get_logger().info("UDP接收线程已启动")
        while self.running:
            # 复制一份连接列表，避免遍历时修改
            connections = list(self.uid_to_connection.values())
            for conn in connections:
                try:
                    # 非阻塞接收
                    conn.socket.setblocking(False)
                    try:
                        data, addr = conn.socket.recvfrom(65536)
                        if data:
                            # 处理收到的UDP数据
                            self._handle_udp_data(conn, data, addr)
                    except (BlockingIOError, socket.error):
                        # 没有可用数据，继续下一个连接
                        pass
                except Exception as e:
                    LoggerFactory.get_logger().error(f"UDP数据接收错误: {e}")
                    LoggerFactory.get_logger().error(traceback.format_exc())

            # 防止CPU占用过高
            time.sleep(0.001)

    def _handle_udp_data(self, conn: UdpSocketConnection, data: bytes, addr):
        """处理收到的UDP数据"""
        # 限速处理
        if conn.speed_limiter and conn.speed_limiter.is_exceed()[0]:
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug('UDP speed limit')
            return

        if conn.speed_limiter:
            conn.speed_limiter.add(len(data))

        # 构造UDP消息并通过WebSocket发送到公网服务端
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_UDP,
            'data': {
                'name': conn.name,
                'data': data,
                'uid': conn.uid,
                'ip_port': conn.ip_port
            }
        }

        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug(f'send udp to WebSocket，uid: {conn.uid}, len: {len(data)}')

        try:
            self.ws.send(NatSerialization.dumps(send_message, ContextUtils.get_password(), self.compress_support), websocket.ABNF.OPCODE_BINARY)
        except Exception as e:
            LoggerFactory.get_logger().error(f"send UDP to WebSocket error: {e}")

    def create_udp_socket(self, name: str, uid: bytes, ip_port: str, speed_limiter: SpeedLimiter) -> bool:
        """创建UDP套接字"""
        if uid in self.uid_to_connection:
            return True

        with self.lock:
            if uid in self.uid_to_connection:  # 再次检查是否已存在
                return True

            try:
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'send UDP socket {name}, {uid}')

                # 创建UDP套接字
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

                # 创建连接对象
                connection = UdpSocketConnection(uid, s, name, ip_port, self.ws, speed_limiter)
                self.uid_to_connection[uid] = connection

                LoggerFactory.get_logger().info(f'UDP socket create success，name: {name}, uid: {uid}')
                return True
            except Exception as e:
                LoggerFactory.get_logger().error(f"udp socket create failed: {e}")
                LoggerFactory.get_logger().error(traceback.format_exc())
                return False

    def send_by_uid(self, uid: bytes, data: bytes):
        """通过UID发送UDP数据"""
        connection = self.uid_to_connection.get(uid)
        if not connection:
            LoggerFactory.get_logger().warning(f"udp UID {uid} not found")
            return False

        try:
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'start send UDP data，UID: {uid}, len: {len(data)}')

            if connection.target_address:
                connection.socket.sendto(data, connection.target_address)
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'UDP send success，target: {connection.target_address}, len: {len(data)}')
                return True
            else:
                LoggerFactory.get_logger().warning(f"UDP has no target address，UID: {uid}")
                return False
        except Exception as e:
            LoggerFactory.get_logger().error(f"UDP data send failed: {e}")
            LoggerFactory.get_logger().error(traceback.format_exc())
            return False

    def close_connection(self, uid: bytes):
        """关闭UDP连接"""
        with self.lock:
            if uid in self.uid_to_connection:
                connection = self.uid_to_connection.pop(uid)
                try:
                    connection.socket.close()
                    LoggerFactory.get_logger().info(f'UDP closed，UID: {uid}')
                except Exception as e:
                    LoggerFactory.get_logger().error(f" close UDP failed: {e}")
            else:
                LoggerFactory.get_logger().warning(f"UID {uid} UDP not found")

    def close(self):
        """关闭所有UDP连接"""
        self.running = False
        with self.lock:
            for uid, conn in list(self.uid_to_connection.items()):
                try:
                    conn.socket.close()
                except Exception:
                    pass
            self.uid_to_connection.clear()