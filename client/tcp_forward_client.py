import ast
import json
import select
import socket
from threading import Thread, Lock
from typing import List, Dict, Tuple, Set

import websocket

from common.logger_factory import LoggerFactory
from constant.message_type_constnat import MessageTypeConstant
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
            with self.lock:
                s_list = list(self.uid_to_socket.values())
                if not s_list:
                    continue
                try:
                    rs, write_s, es = select.select(s_list, s_list, s_list, 5)
                except ValueError:
                    LoggerFactory.get_logger().error('value error continue')
            for each in rs:
                uid = self.socket_to_uid[each]
                try:
                    recv = each.recv(1024)
                except OSError:
                    continue
                send_message: MessageEntity = {
                    'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
                    'data': {
                        'name': self.uid_to_name[uid],
                        'data': recv.hex(),
                        'uid': uid
                    }
                }
                self.ws.send(json.dumps(send_message))
                if not recv:
                    self.uid_to_socket.pop(uid)
                    self.socket_to_uid.pop(each)
                    each.close()

    def create_socket(self, name: str, uid: str):
        with self.lock:
            if uid not in self.uid_to_socket:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(self.name_to_addr[name])
                self.uid_to_socket[uid] = s
                self.socket_to_uid[s] = uid
                self.uid_to_name[uid] = name

    def close(self):
        with self.lock:
            for uid, s in self.uid_to_socket.items():
                s.close()
            self.uid_to_socket.clear()
            self.socket_to_uid.clear()
            self.uid_to_name.clear()
            self.is_running = False

    def send_by_uid(self, uid, msg: bytes):
        self.uid_to_socket[uid].send(msg)
