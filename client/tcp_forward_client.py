import select
import socket
import time
import traceback
from threading import Lock
from typing import Dict, Tuple

import websocket

from common.nat_serialization import NatSerialization
from common.logger_factory import LoggerFactory
from constant.message_type_constnat import MessageTypeConstant
from constant.system_constant import SystemConstant
from entity.message.message_entity import MessageEntity


class TcpForwardClient:
    def __init__(self, name_to_addr: Dict[str, Tuple[str, int]], ws: websocket):
        self.uid_to_socket: Dict[str, socket.socket] = dict()
        self.socket_to_uid: Dict[socket.socket, str] = dict()
        self.name_to_addr: Dict[str, Tuple[str, int]] = name_to_addr
        self.uid_to_name: Dict[str, str] = dict()
        self.is_running = True
        self.ws = ws
        self.lock = Lock()

    def start_forward(self):
        while self.is_running:
            with self.lock:  # with
                s_list = list(self.uid_to_socket.values())
                if not s_list:
                    continue
                try:
                    rs, write_s, es = select.select(s_list, s_list, s_list, 5)
                except ValueError:
                    LoggerFactory.get_logger().error('value error continue')
                    continue
            for each in rs:
                uid = self.socket_to_uid[each]
                try:
                    recv = each.recv(SystemConstant.CHUNK_SIZE)
                except OSError:
                    continue
                send_message: MessageEntity = {
                    'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
                    'data': {
                        'name': self.uid_to_name[uid],
                        'data': recv,
                        'uid': uid
                    }
                }
                start_time = time.time()
                self.ws.send(NatSerialization.dumps(send_message),  websocket.ABNF.OPCODE_BINARY)
                LoggerFactory.get_logger().debug(f'send to ws cost time {time.time() - start_time}')
                if not recv:
                    try:
                        self.close_connection(each)
                    except Exception:
                        LoggerFactory.get_logger().error(f'close error: {traceback.format_exc()}')
                    # self.uid_to_socket.pop(uid)
                    # self.socket_to_uid.pop(each)
                    # each.close()

    def create_socket(self, name: str, uid: str):
        with self.lock:
            if uid not in self.uid_to_socket:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(self.name_to_addr[name])
                self.uid_to_socket[uid] = s
                self.socket_to_uid[s] = uid
                self.uid_to_name[uid] = name

    def close_connection(self, socket_client: socket.socket):
        uid = self.socket_to_uid.pop(socket_client)
        self.uid_to_socket.pop(uid)
        socket_client.close()

    def close(self):
        with self.lock:
            for uid, s in self.uid_to_socket.items():
                s.close()
            self.uid_to_socket.clear()
            self.socket_to_uid.clear()
            self.uid_to_name.clear()
            self.is_running = False

    def send_by_uid(self, uid, msg: bytes):
        s = self.uid_to_socket[uid]
        if not msg:
            LoggerFactory.get_logger().info('get empty message, close')
            self.close_connection(s)
        s.send(msg)
