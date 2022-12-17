import asyncio
import select
import socket
import time
import traceback
import uuid
from functools import partial
from threading import Thread, Lock
from typing import Dict

from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.pool import EPool, SelectPool
from constant.message_type_constnat import MessageTypeConstant
from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity

has_epool = hasattr(select, 'epoll')
# has_epool = False


class TcpForwardClient:
    def __init__(self, websocket_handler: 'MyWebSocketaHandler', name: str, listen_port: int, loop, tornado_loop, ip_port: str):
        self.close_lock = Lock()
        from server.websocket_handler import MyWebSocketaHandler
        self.websocket_handler = websocket_handler  # type: MyWebSocketaHandler
        self.name: str = name
        self.listen_port: int = listen_port
        self.is_running: bool = True
        self.socket: socket.socket = None
        self.uid_to_client: Dict[str, socket.socket] = dict()
        self.client_to_uid: Dict[socket.socket, str] = dict()
        # self.fileno_to_client: Dict[int, socket.socket] = dict()
        self.tornado_loop = tornado_loop
        self.socket_event_loop = EPool() if has_epool else SelectPool()
        self.socket_event_loop.add_callback_function(self.handle_message)
        self.ip_port: str = ip_port

    def handle_message(self, each: socket.socket):
        # 发送到websocket
        each: socket.socket
        try:
            recv = each.recv(SystemConstant.CHUNK_SIZE)
        except ConnectionResetError:
            recv = b''
        uid = self.client_to_uid[each]
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
            'data': {
                'name': self.name,
                'data': recv,
                'uid': uid,
                'ip_port': self.ip_port
            }
        }
        if not recv:
            LoggerFactory.get_logger().info('recv empty, close')
            try:
                self.close_connection(each)
            except (OSError, ValueError, KeyError):
                LoggerFactory.get_logger().error(f'close error: {traceback.format_exc()}')
        try:
            self.tornado_loop.add_callback(
                partial(self.websocket_handler.write_message, NatSerialization.dumps(send_message, ContextUtils.get_password())), True)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def start_accept(self):
        LoggerFactory.get_logger().info(f'start accept {self.listen_port}')
        self.socket_event_loop.is_running = True
        Thread(target=self.socket_event_loop.run).start()
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
                self.socket_event_loop.register(client)

    async def send_to_socket(self, uid: str, message: bytes):
        send_start_time = time.time()
        if uid not in self.uid_to_client:
            LoggerFactory.get_logger().debug(f'{message}, {uid} not in ')
            return
        socket_client = self.uid_to_client[uid]
        try:
            await asyncio.get_event_loop().sock_sendall(socket_client, message)
        except OSError:
            LoggerFactory.get_logger().warn(f'{uid} os error')
            pass
        if not message:
            asyncio.get_event_loop().run_in_executor(None, self.close_connection, socket_client)

        LoggerFactory.get_logger().debug(f'send to socket cost time {time.time() - send_start_time}')

    def close_connection(self, socket_client: socket.socket):
        # todo
        LoggerFactory.get_logger().info(f'close {socket_client}')
        with self.close_lock:
            self.socket_event_loop.unregister(socket_client)
            if socket_client not in self.client_to_uid:
                return
            uid = self.client_to_uid.pop(socket_client)
            self.uid_to_client.pop(uid)
        socket_client.close()

    def bind_port(self):
        self.socket: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(('', self.listen_port))
        self.socket.listen(5)

    def close(self):
        self.is_running = False
        self.socket_event_loop.stop()
        # close client
        try:
            for client, uid in self.client_to_uid.items():
                try:
                    client.close()
                except Exception:
                    LoggerFactory.get_logger().warn(f'close error, {client}, {traceback.format_exc()}')
                try:
                    self.socket_event_loop.unregister(client)
                except Exception:
                    LoggerFactory.get_logger().warn(f'unregister error, {client}, {traceback.format_exc()}')
        except Exception:
            LoggerFactory.get_logger().warning(f'close error: {traceback.format_exc()}')
        self.client_to_uid.clear()
        self.uid_to_client.clear()
        # close server
        if self.socket:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.socket.close()
            self.socket = None
