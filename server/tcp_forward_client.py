import asyncio
import logging
import socket
import time
import traceback
import uuid
from collections import defaultdict
from functools import partial
from threading import Thread, Lock
from asyncio import Lock as AsyncioLock
from typing import Dict, Tuple, List, Set
import os

import tornado

from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.pool import SelectPool
from common.register_append_data import ResisterAppendData
from common.speed_limit import SpeedLimiter
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class PublicSocketServer:
    """在公网上监听的端口"""

    def __init__(self, s: socket.socket, name: str, ip_port: str, websocket_handler: 'MyWebSocketaHandler', speed_limit_size: float):
        self.socket_server = s
        self.name: str = name
        self.ip_port: str = ip_port
        self.websocket_handler: 'MyWebSocketaHandler' = websocket_handler
        self.speed_limit_size: float = speed_limit_size
        self.client_set: Set['PublicSocketConnection'] = set()

    def add_client(self, client: 'PublicSocketConnection'):
        self.client_set.add(client)

    def delete_client(self, client: 'PublicSocketConnection'):
        self.client_set.remove(client)

    def __str__(self):
        return f'{self.name}_{self.ip_port}'


class PublicSocketConnection:
    """对应连接公网端口的客户端"""

    def __init__(self, uid: bytes, s: socket.socket, socket_server: PublicSocketServer):
        self.uid: bytes = uid
        self.socket: socket.socket = s
        self.socket_server = socket_server
        self.socket_server.add_client(self)

    def __str__(self):
        return f'{self.uid}_{self.socket}_{self.socket_server.name}'


class TcpForwardClient:
    _instance = None

    def __init__(self, loop, tornado_loop):
        self.socket_event_loop = SelectPool()
        self.listen_socket_to_public_server: Dict[socket.socket, PublicSocketServer] = dict()
        self.client_name_to_public_server_set: Dict[str, Set[PublicSocketServer]] = defaultdict(set)
        self.uid_to_connection: Dict[bytes, PublicSocketConnection] = dict()  # uid 对应的连接公网端口的客户端
        self.socket_to_connection: Dict[socket.socket, PublicSocketConnection] = dict()
        self.tornado_loop = tornado_loop
        self.close_lock = Lock()
        self.client_name_to_lock: Dict[str, AsyncioLock] = dict()

    @classmethod
    def get_instance(cls) -> 'TcpForwardClient':
        if cls._instance is None:
            cls._instance = cls(asyncio.get_event_loop(), tornado.ioloop.IOLoop.current())
            Thread(target=cls._instance.socket_event_loop.run).start()
        return cls._instance

    async def register_listen_server(self, s: socket.socket, name: str, ip_port: str, websocket_handler: 'MyWebSocketaHandler', speed_limit_size: float):
        client_name = websocket_handler.client_name
        if client_name not in self.client_name_to_lock:
            self.client_name_to_lock[client_name] = AsyncioLock()
        async with self.client_name_to_lock.get(client_name):
            append_data = ResisterAppendData(self.start_accept, SpeedLimiter(speed_limit_size) if speed_limit_size else None)
            server = PublicSocketServer(s, name, ip_port, websocket_handler, speed_limit_size)
            self.listen_socket_to_public_server[s] = server
            self.client_name_to_public_server_set[client_name].add(server)
            self.socket_event_loop.register(s, append_data)

    async def close_by_client_name(self, client_name: str):
        if client_name not in self.client_name_to_public_server_set:
            return
        if client_name not in self.client_name_to_lock:
            self.client_name_to_lock[client_name] = AsyncioLock()
        async with self.client_name_to_lock.get(client_name):
            client_connection_list: List[PublicSocketConnection] = []
            server_socket_list = []

            for server in self.client_name_to_public_server_set[client_name]:
                for c in server.client_set:
                    client_connection_list.append(c)
                server_socket_list.append(server)
            for c in client_connection_list:
                c.socket.close()
                self.socket_event_loop.unregister(c.socket)
                self.socket_to_connection.pop(c.socket)
                self.uid_to_connection.pop(c.uid)
            for s in server_socket_list:
                self.listen_socket_to_public_server.pop(s.socket_server)
                try:
                    self.socket_event_loop.unregister(s.socket_server)
                    s.socket_server.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.socket_server.close()
            self.client_name_to_public_server_set.pop(client_name)
        self.client_name_to_lock.pop(client_name)

    def handle_message(self, each: socket.socket, data: ResisterAppendData):
        # 发送到websocket
        each: socket.socket
        if data.speed_limiter and data.speed_limiter.is_exceed()[0]:
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug('over speed')
            self.socket_event_loop.unregister_and_register_delay(each, data, 1)
        try:
            recv = each.recv(data.read_size)
            if data.speed_limiter:
                data.speed_limiter.add(len(recv))
        except ConnectionResetError:
            recv = b''
        # client = self.uid_to_client[uid]
        if each not in self.socket_to_connection:
            return
        socket_connection = self.socket_to_connection[each]
        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug(f'send to ws uid: {socket_connection.uid}, len: {len(recv)}')
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
            'data': {
                'name': socket_connection.socket_server.name,
                'data': recv,
                'uid': socket_connection.uid,
                'ip_port': socket_connection.socket_server.ip_port
            }
        }
        if not recv:
            LoggerFactory.get_logger().info('recv empty, close')
            try:
                self.close_connection(socket_connection)
            except (OSError, ValueError, KeyError):
                LoggerFactory.get_logger().error(f'close error: {traceback.format_exc()}')
        try:
            is_compress = socket_connection.socket_server.websocket_handler.compress_support
            self.tornado_loop.add_callback(
                partial(socket_connection.socket_server.websocket_handler.write_message, NatSerialization.dumps(send_message, ContextUtils.get_password(), is_compress)), True)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def start_accept(self, server_socket: socket, register_append_data: ResisterAppendData):
        try:
            client, address = server_socket.accept()
            client: socket.socket
        except OSError:
            return
        LoggerFactory.get_logger().info(f'get connect : {address}')
        server = self.listen_socket_to_public_server[server_socket]
        # 当前 服务端的client 也会对应服务端连接内网服务的一个 client
        uid = os.urandom(4)
        client_socket_connection = PublicSocketConnection(uid, client, server)
        self.uid_to_connection[uid] = client_socket_connection
        self.socket_to_connection[client] = client_socket_connection
        Thread(target=self.request_to_connect, args=(client_socket_connection,)).start()
        append_data = ResisterAppendData(self.handle_message, register_append_data.speed_limiter)
        self.socket_event_loop.register(client, append_data)

    def request_to_connect(self, client_socket_connection: PublicSocketConnection):
        """请求连接客户端"""
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.REQUEST_TO_CONNECT,
            'data': {
                'name': client_socket_connection.socket_server.name,
                'data': ''.encode(),
                'uid': client_socket_connection.uid,
                'ip_port': client_socket_connection.socket_server.ip_port
            }
        }
        self.tornado_loop.add_callback(
            partial(client_socket_connection.socket_server.websocket_handler.write_message, NatSerialization.dumps(send_message, ContextUtils.get_password(), False)), True
        )

    async def send_to_socket(self, uid: bytes, message: bytes):
        send_start_time = time.time()
        if uid not in self.uid_to_connection:
            LoggerFactory.get_logger().debug(f'{message}, {uid} not in ')
            return
        connection = self.uid_to_connection[uid]
        socket_client = connection.socket
        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug(f'send to socket uid: {uid}, len: {len(message)}')
        try:
            await asyncio.get_event_loop().sock_sendall(socket_client, message)
        except OSError:
            LoggerFactory.get_logger().warn(f'{uid} os error')
            pass
        if not message:
            asyncio.get_event_loop().run_in_executor(None, self.close_connection, connection)

        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug(f'send to socket cost time {time.time() - send_start_time}')

    def close_connection(self, connection: PublicSocketConnection):
        try:
            LoggerFactory.get_logger().info(f'close {connection.uid}')
            # with self.close_lock:
            uid = connection.uid
            if uid not in self.uid_to_connection:
                return
            self.socket_event_loop.unregister(connection.socket)
            self.uid_to_connection.pop(uid)
            self.socket_to_connection.pop(connection.socket)
            connection.socket_server.delete_client(connection)
            connection.socket.close()
        except Exception:
            LoggerFactory.get_logger().error(f'close error {traceback.format_exc()}')
            raise

    @classmethod
    def create_listen_socket(cls, port):
        s: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('', port))
        s.listen(5)
        return s

    def close(self):
        """其实没有close， 程序启动的时候start"""
        # self.is_running = False
        self.socket_event_loop.stop()
        # close client
        # try:
        #     for client, uid in self.client_to_uid.items():
        #         try:
        #             client.close()
        #         except Exception:
        #             LoggerFactory.get_logger().warn(f'close error, {client}, {traceback.format_exc()}')
        #         try:
        #             self.socket_event_loop.unregister(client)
        #         except Exception:
        #             LoggerFactory.get_logger().warn(f'unregister error, {client}, {traceback.format_exc()}')
        # except Exception:
        #     LoggerFactory.get_logger().warning(f'close error: {traceback.format_exc()}')
        # self.client_to_uid.clear()
        # self.uid_to_client.clear()
        # close server
        # if self.socket:
        #     try:
        #         self.socket.shutdown(socket.SHUT_RDWR)
        #     except OSError:
        #         pass
        #     self.socket.close()
        #     self.socket = None
