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
    """Listening port on public network"""

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
    """Client connecting to public network port"""

    def __init__(self, uid: bytes, s: socket.socket, socket_server: PublicSocketServer):
        self.uid: bytes = uid
        self.socket: socket.socket = s
        self.socket_server = socket_server
        self.socket_server.add_client(self)

        # Optimistic send mode configuration
        self.optimistic_mode = True  # Enable optimistic send by default
        self.connection_confirmed = False  # Whether connection is confirmed
        self.early_data_buffer = []  # Buffer early data (data received before connection confirmation)
        self.max_early_data_packets = 10  # Maximum 10 buffered packets

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
                await self.socket_event_loop.async_unregister(c.socket)
                self.socket_to_connection.pop(c.socket)
                self.uid_to_connection.pop(c.uid)
            for s in server_socket_list:
                self.listen_socket_to_public_server.pop(s.socket_server)
                try:
                    await self.socket_event_loop.async_unregister(s.socket_server)
                    s.socket_server.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.socket_server.close()
            self.client_name_to_public_server_set.pop(client_name)
        self.client_name_to_lock.pop(client_name)

    def handle_message(self, each: socket.socket, data: ResisterAppendData):
        # 发送到websocket
        each: socket.socket
        try:
            recv = each.recv(data.read_size)
        except ConnectionResetError:
            recv = b''
        # client = self.uid_to_client[uid]
        if each not in self.socket_to_connection:
            return
        socket_connection = self.socket_to_connection[each]

        # Optimistic send mode: buffer data if connection not confirmed
        if socket_connection.optimistic_mode and not socket_connection.connection_confirmed:
            if len(socket_connection.early_data_buffer) < socket_connection.max_early_data_packets:
                socket_connection.early_data_buffer.append(recv)
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'Buffering early data uid: {socket_connection.uid}, len: {len(recv)}, buffer_size: {len(socket_connection.early_data_buffer)}')
            else:
                # Too much buffered data, connection may have failed, close connection
                LoggerFactory.get_logger().warning(f'Too much early data buffered, closing connection uid: {socket_connection.uid}')
                try:
                    self.close_connection(socket_connection)
                except (OSError, ValueError, KeyError):
                    LoggerFactory.get_logger().error(f'Close error: {traceback.format_exc()}')
            return

        # --- 发送端限速：在发送前等待 ---
        if data.speed_limiter and recv:
            wait_time = data.speed_limiter.acquire(len(recv))
            if wait_time > 0:
                time.sleep(wait_time)

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
            handler = socket_connection.socket_server.websocket_handler
            is_compress = handler.compress_support
            protocol_version = handler.protocol_version
            self.tornado_loop.add_callback(
                partial(handler.write_message, NatSerialization.dumps(send_message, ContextUtils.get_password(), is_compress, protocol_version)), True)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def start_accept(self, server_socket: socket, register_append_data: ResisterAppendData):
        try:
            client, address = server_socket.accept()
            client: socket.socket
            # 启用 TCP_NODELAY 减少延迟（禁用 Nagle 算法）
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            return
        LoggerFactory.get_logger().info(f'Received connection from: {address}')
        server = self.listen_socket_to_public_server[server_socket]
        # Current server client also corresponds to a client connecting to internal network service
        uid = os.urandom(4)
        client_socket_connection = PublicSocketConnection(uid, client, server)
        self.uid_to_connection[uid] = client_socket_connection
        self.socket_to_connection[client] = client_socket_connection
        Thread(target=self.request_to_connect, args=(client_socket_connection,)).start()
        append_data = ResisterAppendData(self.handle_message, register_append_data.speed_limiter)
        self.socket_event_loop.register(client, append_data)

    def request_to_connect(self, client_socket_connection: PublicSocketConnection):
        """Request connection to client"""
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.REQUEST_TO_CONNECT,
            'data': {
                'name': client_socket_connection.socket_server.name,
                'data': ''.encode(),
                'uid': client_socket_connection.uid,
                'ip_port': client_socket_connection.socket_server.ip_port
            }
        }
        handler = client_socket_connection.socket_server.websocket_handler
        self.tornado_loop.add_callback(
            partial(handler.write_message, NatSerialization.dumps(send_message, ContextUtils.get_password(), handler.compress_support, handler.protocol_version)), True
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
            # 添加超时机制
            await asyncio.wait_for(asyncio.get_event_loop().sock_sendall(socket_client, message), timeout=30)
        except asyncio.TimeoutError:
            LoggerFactory.get_logger().warn(f"Socket send timeout for {uid}, closing connection")
            # 使用 ensure_future 替代 create_task，兼容 Python 3.6
            asyncio.ensure_future(self.close_connection_async(connection))
            return
        except OSError as e:
            LoggerFactory.get_logger().warn(f'{uid} os error: {e}')
            # 使用 ensure_future 替代 create_task，兼容 Python 3.6
            asyncio.ensure_future(self.close_connection_async(connection))
            return
        if not message:
            # 使用异步方式关闭连接
            asyncio.ensure_future(self.close_connection_async(connection))
        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug(f'send to socket cost time {time.time() - send_start_time}')

    async def close_connection_async(self, connection: PublicSocketConnection):
        """Asynchronously close connection to avoid blocking in event loop"""
        try:
            LoggerFactory.get_logger().info(f'Async closing connection {connection.uid}')
            uid = connection.uid
            if uid not in self.uid_to_connection:
                return
            # Remove from tracking dictionaries
            self.uid_to_connection.pop(uid, None)
            self.socket_to_connection.pop(connection.socket, None)
            connection.socket_server.delete_client(connection)
            # Ensure unregister before closing
            try:
                await self.socket_event_loop.async_unregister(connection.socket)
            except Exception as e:
                LoggerFactory.get_logger().error(f'Error unregistering socket: {e}')

            # Close socket
            try:
                connection.socket.close()
            except Exception as e:
                LoggerFactory.get_logger().error(f'Error closing socket: {e}')
        except Exception as e:
            LoggerFactory.get_logger().error(f'Close error: {e}')

    def close_connection(self, connection: PublicSocketConnection):
        try:
            LoggerFactory.get_logger().info(f'Closing connection {connection.uid}')
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
            LoggerFactory.get_logger().error(f'Close error: {traceback.format_exc()}')
            raise

    @classmethod
    def create_listen_socket(cls, port):
        s: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('', port))
        s.listen(5)
        return s

    def close(self):
        """Actually no close operation, starts when program starts"""
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
