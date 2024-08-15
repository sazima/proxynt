import logging
import socket
import time
import traceback
from threading import Lock
from typing import Dict

from common import websocket
from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.pool import SelectPool
from common.register_append_data import ResisterAppendData
from common.speed_limit import SpeedLimiter
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class PrivateSocketConnection:
    """连接内网端口的客户端"""

    def __init__(self, uid: bytes, s: socket.socket, name: str):
        self.uid: bytes = uid
        self.socket: socket.socket = s
        self.name: str = name


class TcpForwardClient:
    def __init__(self, ws: websocket, compress_support: bool):
        self.uid_to_socket_connection: Dict[bytes, PrivateSocketConnection] = dict()
        self.socket_to_socket_connection: Dict[socket.socket, PrivateSocketConnection] = dict()
        self.compress_support: bool = compress_support
        self.ws = ws
        self.lock = Lock()

        self.socket_event_loop = SelectPool()

    def set_running(self, running: bool):
        self.socket_event_loop.is_running = running

    def start_forward(self):
        self.socket_event_loop.run()

    def handle_message(self, each: socket.socket, data: ResisterAppendData):
        connection = self.socket_to_socket_connection[each]
        if data.speed_limiter and data.speed_limiter.is_exceed()[0]:
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug('over speed')
            self.socket_event_loop.unregister_and_register_delay(each, data, 1)
        try:
            recv = each.recv(data.read_size)
            if data.speed_limiter:
                data.speed_limiter.add(len(recv))
        except OSError as e:
            LoggerFactory.get_logger().error(f'{traceback.format_exc()}')
            LoggerFactory.get_logger().warn(f'get os error: {str(e)}, close')
            recv = b''
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
            'data': {
                'name': connection.name,
                'data': recv,
                'uid': connection.uid,
                'ip_port': ''  # 这个对服务端没有用, 因此写了个空
            }
        }
        start_time = time.time()
        self.ws.send(NatSerialization.dumps(send_message, ContextUtils.get_password(), self.compress_support), websocket.ABNF.OPCODE_BINARY)
        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug(f'send to ws  uid: {connection.uid} len {len(recv)} , cost time {time.time() - start_time}')
        if not recv:
            try:
                self.close_connection(each)
            except Exception:
                LoggerFactory.get_logger().error(f'close error: {traceback.format_exc()}')

    def create_socket(self, name: str, uid: bytes, ip_port: str, speed_limiter: SpeedLimiter) -> bool:
        if uid in self.uid_to_socket_connection:
            return True
        connection = None
        with self.lock:
            if uid in self.uid_to_socket_connection:  # 再次判断
                return True
            try:
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'create socket {name}, {uid}')
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5)

                connection = PrivateSocketConnection(uid, s, name)
                self.socket_to_socket_connection[s] = connection
                ip, port = ip_port.split(':')
                try:
                    s.connect((ip, int(port)))
                except OSError as e:
                    LoggerFactory.get_logger().info(f'connection error, {e}')
                    self.close_connection(s)
                    self.close_remote_socket(connection)
                    return False
                self.uid_to_socket_connection[uid] = connection
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'register socket {name}, {uid}')
                self.socket_event_loop.register(s, ResisterAppendData(self.handle_message, speed_limiter))
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'register socket success {name}, {uid}')
                return True
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
                if connection:
                    self.close_remote_socket(connection)
                return False

    def close_connection(self, socket_client: socket.socket):
        LoggerFactory.get_logger().info(f'close {socket_client}')
        if socket_client in self.socket_to_socket_connection:
            connection: PrivateSocketConnection = self.socket_to_socket_connection.pop(socket_client)
            self.socket_event_loop.unregister(socket_client)
            try:
                socket_client.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                LoggerFactory.get_logger().warn(f'shutdown OS error {e}')
            socket_client.close()
            LoggerFactory.get_logger().info(f'close success {socket_client}')
            if connection.uid in self.uid_to_socket_connection:
                self.uid_to_socket_connection.pop(connection.uid)
            self.close_remote_socket(connection)

    def close(self):
        with self.lock:
            self.socket_event_loop.stop()
            for uid, c in self.uid_to_socket_connection.items():
                s = c.socket
                try:
                    self.socket_event_loop.unregister(s)
                except Exception:
                    LoggerFactory.get_logger().error(traceback.format_exc())
                try:
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError as e:
                        LoggerFactory.get_logger().warn(f'shutdown OS error {e}')
                    s.close()
                except Exception:
                    LoggerFactory.get_logger().error(traceback.format_exc())
            self.uid_to_socket_connection.clear()
            self.socket_to_socket_connection.clear()
            self.set_running(False)
            self.socket_event_loop.clear()

    def close_remote_socket(self, connection: PrivateSocketConnection):
        # if name is None:
        #     connection = self.uid_to_socket_connection.get(uid)
        #     if not connection:
        #         return
        name = connection.name
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
            'data': {
                'name': name,
                'data': b'',
                'uid': connection.uid,
                'ip_port': ''
            }
        }
        start_time = time.time()
        self.ws.send(NatSerialization.dumps(send_message, ContextUtils.get_password(), self.compress_support), websocket.ABNF.OPCODE_BINARY)
        LoggerFactory.get_logger().debug(f'send to ws cost time {time.time() - start_time}')

    def send_by_uid(self, uid: bytes, msg: bytes):
        connection = self.uid_to_socket_connection.get(uid)
        if not connection:
            return
        try:
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'start send to  socket uid: {uid}, {len(msg)}')
            s = connection.socket
            s.sendall(msg)
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'finish send to socket uid {uid}, {len(msg)}')
            if not msg:
                self.close_connection(s)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
            # 出错后, 发送空, 服务器会关闭
            self.close_remote_socket(connection)
