import asyncio
import logging
import socket
import threading
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
from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class TcpForwardClient:
    def __init__(self, ws: websocket):
        self.uid_to_socket: Dict[bytes, socket.socket] = dict()
        self.socket_to_uid: Dict[socket.socket, bytes] = dict()
        # self.name_to_addr: Dict[str, Tuple[str, int]] = name_to_addr
        self.uid_to_name: Dict[bytes, str] = dict()
        self.is_running = True
        self.ws = ws
        self.lock = Lock()

        self.socket_event_loop = SelectPool()

    def start_forward(self):
        self.socket_event_loop.is_running = True
        self.socket_event_loop.run()

    def handle_message(self, each: socket.socket, data: ResisterAppendData):
        uid = self.socket_to_uid[each]
        if data.speed_limiter and data.speed_limiter.is_exceed()[0]:
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug('over speed')
            self.socket_event_loop.unregister_and_register_delay(each, data, 1)
        try:
            recv = each.recv(data.read_size)
            if data.speed_limiter:
                data.speed_limiter.add(len(recv))
        except OSError:
            return
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
            'data': {
                'name': self.uid_to_name[uid],
                'data': recv,
                'uid': uid,
                'ip_port': ''  # 这个对服务端没有用, 因此写了个空
            }
        }
        start_time = time.time()
        self.ws.send(NatSerialization.dumps(send_message, ContextUtils.get_password()), websocket.ABNF.OPCODE_BINARY)
        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug(f'send to ws cost time {time.time() - start_time}')
        if not recv:
            try:
                self.close_connection(each)
            except Exception:
                LoggerFactory.get_logger().error(f'close error: {traceback.format_exc()}')

    def create_socket(self, name: str, uid: bytes, ip_port: str, speed_limiter: SpeedLimiter):
        if uid in self.uid_to_socket:
            return
        with self.lock:
            if uid in self.uid_to_socket:  # 再次判断
                return
            try:
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'create socket {name}, {uid}')
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5)
                ip, port = ip_port.split(':')
                s.connect((ip, int(port)))
                self.uid_to_socket[uid] = s  #
                self.uid_to_name[uid] = name
                self.socket_to_uid[s] = uid
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'register socket {name}, {uid}')
                self.socket_event_loop.register(s, ResisterAppendData(self.handle_message, speed_limiter))
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'register socket success {name}, {uid}')
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
                self.close_remote_socket(uid, name)

    def close_connection(self, socket_client: socket.socket):
        LoggerFactory.get_logger().info(f'close {socket_client}')
        if socket_client in self.socket_to_uid:
            uid = self.socket_to_uid.pop(socket_client)
            self.uid_to_socket.pop(uid)
            self.socket_event_loop.unregister(socket_client)
            socket_client.close()
            LoggerFactory.get_logger().info(f'close success {socket_client}')

    def close(self):
        with self.lock:
            self.socket_event_loop.stop()
            for uid, s in self.uid_to_socket.items():
                try:
                    s.close()
                except Exception:
                    LoggerFactory.get_logger().error(traceback.format_exc())
                try:
                    self.socket_event_loop.unregister(s)
                except Exception:
                    LoggerFactory.get_logger().error(traceback.format_exc())
            self.uid_to_socket.clear()
            self.socket_to_uid.clear()
            self.uid_to_name.clear()
            self.is_running = False

    def close_remote_socket(self, uid: bytes, name: str = None):
        if name is None:
            name = self.uid_to_name.get(uid, '')
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
            'data': {
                'name': name,
                'data': b'',
                'uid': uid,
                'ip_port': ''
            }
        }
        start_time = time.time()
        self.ws.send(NatSerialization.dumps(send_message, ContextUtils.get_password()), websocket.ABNF.OPCODE_BINARY)
        LoggerFactory.get_logger().debug(f'send to ws cost time {time.time() - start_time}')

    def send_by_uid(self, uid, msg: bytes):
        try:
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'send to {uid}, {len(msg)}')
            s = self.uid_to_socket[uid]
            s.sendall(msg)
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'send success to {uid}, {len(msg)}')
            if not msg:
                self.close_connection(s)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
            # 出错后, 发送空, 服务器会关闭
            self.close_remote_socket(uid)

