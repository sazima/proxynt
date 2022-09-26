import asyncio
import json
import select
import socket
import traceback
import uuid
from threading import Thread, Lock
from typing import Dict

from common.logger_factory import LoggerFactory
from constant.message_type_constnat import MessageTypeConstant
from entity.message.message_entity import MessageEntity


class TcpForwardClient:
    def __init__(self, websocket_handler: 'MyWebSocketaHandler', name: str, listen_port: int, loop):
        from server.websocket_handler import MyWebSocketaHandler
        self.websocket_handler = websocket_handler  # type: MyWebSocketaHandler
        self.name: str = name
        self.listen_port: int = listen_port
        self.is_running: bool = True
        self.socket: socket.socket = None
        self.uid_to_client: Dict[str, socket.socket] = dict()
        self.client_to_uid: Dict[ socket.socket, str] = dict()
        self.loop = loop
        self.send_lock = Lock()


    def start_listen_message(self):
        asyncio.set_event_loop(self.loop)
        while self.is_running:
            s_list = list(self.client_to_uid.keys())
            if not s_list:
                continue
            rs, ws, es = select.select(s_list, s_list, s_list, 5)
            for each in rs:
                # 发送到websocket
                each: socket.socket
                recv = each.recv(1024)
                uid = self.client_to_uid[each]
                send_message: MessageEntity = {
                    'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
                    'data': {
                        'name': self.name,
                        'data': recv.hex(),
                        'uid': uid
                    }
                }
                if not recv:
                    self.uid_to_client.pop(uid)
                    self.client_to_uid.pop(each)
                    each.close()
                try:
                    # asyncio.ensure_future(self._on_close(code, reason))
                    with self.send_lock:
                        self.websocket_handler.write_message(json.dumps(send_message))
                except Exception:
                    LoggerFactory.get_logger().error(traceback.format_exc())
                    # return
                # if not recv:
                #     return

    def start_accept(self):
        LoggerFactory.get_logger().info(f'start accept {self.listen_port}')
        asyncio.set_event_loop(self.loop)
        Thread(target=self.start_listen_message).start()
        while self.is_running:
            rs, ws, es = select.select([self.socket], [self.socket], [self.socket])
            for each in rs:
                if self.socket is None:
                    continue
                try:
                    client, address = self.socket.accept()
                except OSError:
                    continue
                LoggerFactory.get_logger().info(f'get connect : {address}')
                # 当前 服务端的client 也会对应服务端连接内网服务的一个 client
                uid = uuid.uuid4().hex
                self.uid_to_client[uid] = client
                self.client_to_uid[client] = uid

    def send_to_socket(self, uid: str, message: bytes):
        if uid not in self.uid_to_client:
            return
        self.uid_to_client[uid].send(message)

    def bind_port(self):
        self.socket: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(('', self.listen_port))
        self.socket.listen(5)
        self.socket.setblocking(True)

    def close(self):
        self.is_running = False
        if self.socket:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()
            self.socket = None
