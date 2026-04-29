import asyncio
import logging
import socket
import time
import traceback
import uuid
from collections import defaultdict
from threading import Lock
from asyncio import Lock as AsyncioLock
from typing import Dict, Tuple, List, Set
import os

import tornado

from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
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
        self.connect_requested = False  # Whether REQUEST_TO_CONNECT has been sent
        self.early_data_buffer = []  # Buffer early data (data received before connection confirmation)
        self.max_early_data_packets = 65536 * 4  # Maximum buffered packets

    def __str__(self):
        return f'{self.uid}_{self.socket}_{self.socket_server.name}'


class TcpForwardClient:
    _instance = None

    def __init__(self, loop, tornado_loop):
        self.listen_socket_to_public_server: Dict[socket.socket, PublicSocketServer] = dict()
        self.client_name_to_public_server_set: Dict[str, Set[PublicSocketServer]] = defaultdict(set)
        self.uid_to_connection: Dict[bytes, PublicSocketConnection] = dict()  # uid 对应的连接公网端口的客户端
        self.socket_to_connection: Dict[socket.socket, PublicSocketConnection] = dict()
        self.tornado_loop = tornado_loop
        self.close_lock = Lock()
        self.client_name_to_lock: Dict[str, AsyncioLock] = dict()
        self.uid_to_send_lock: Dict[bytes, AsyncioLock] = dict()  # 防止data/close并发竞态

    @classmethod
    def get_instance(cls) -> 'TcpForwardClient':
        if cls._instance is None:
            cls._instance = cls(asyncio.get_event_loop(), tornado.ioloop.IOLoop.current())
        return cls._instance

    async def register_listen_server(self, s: socket.socket, name: str, ip_port: str, websocket_handler: 'MyWebSocketaHandler', speed_limit_size: float):
        client_name = websocket_handler.client_name
        if client_name not in self.client_name_to_lock:
            self.client_name_to_lock[client_name] = AsyncioLock()
        async with self.client_name_to_lock.get(client_name):
            server = PublicSocketServer(s, name, ip_port, websocket_handler, speed_limit_size)
            self.listen_socket_to_public_server[s] = server
            self.client_name_to_public_server_set[client_name].add(server)
            # 启动 asyncio accept 循环（Python 3.6 兼容）
            asyncio.ensure_future(self._accept_loop(server))

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
                self.socket_to_connection.pop(c.socket, None)
                self.uid_to_connection.pop(c.uid, None)
            for s in server_socket_list:
                self.listen_socket_to_public_server.pop(s.socket_server, None)
                try:
                    s.socket_server.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.socket_server.close()
            self.client_name_to_public_server_set.pop(client_name)
        self.client_name_to_lock.pop(client_name)

    async def _accept_loop(self, server: PublicSocketServer):
        """Accept loop for incoming connections (asyncio coroutine)"""
        loop = asyncio.get_event_loop()
        server_socket = server.socket_server

        try:
            while True:
                try:
                    client, address = await loop.sock_accept(server_socket)
                    # 启用 TCP_NODELAY 减少延迟
                    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                    LoggerFactory.get_logger().info(f'Received connection from: {address}')

                    # 创建连接对象
                    uid = os.urandom(4)
                    client_socket_connection = PublicSocketConnection(uid, client, server)
                    self.uid_to_connection[uid] = client_socket_connection
                    self.socket_to_connection[client] = client_socket_connection

                    # 启动转发协程（Python 3.6 兼容）
                    asyncio.ensure_future(self._forward_socket_to_ws(client_socket_connection))

                except OSError as e:
                    LoggerFactory.get_logger().error(f'Accept error: {e}')
                    break
        except Exception as e:
            LoggerFactory.get_logger().error(f'Accept loop error: {e}')
            LoggerFactory.get_logger().error(traceback.format_exc())

    async def _forward_socket_to_ws(self, connection: PublicSocketConnection):
        """从公网 socket 读取，转发到客户端 WebSocket（asyncio 协程）"""
        loop = asyncio.get_event_loop()
        sock = connection.socket
        handler = connection.socket_server.websocket_handler
        speed_limit_size = connection.socket_server.speed_limit_size

        limiter = SpeedLimiter(speed_limit_size) if speed_limit_size > 0 else None

        try:
            # Optimistic send mode: wait for connection confirmation
            if connection.optimistic_mode and not connection.connection_confirmed:
                # Buffer early data until connection is confirmed
                while not connection.connection_confirmed:
                    try:
                        data = await asyncio.wait_for(loop.sock_recv(sock, 65536), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue

                    if not data:
                        # Client closed before confirmation
                        LoggerFactory.get_logger().info(f'recv empty before confirmed, close uid: {connection.uid.hex()}')
                        await self.close_connection_async(connection)
                        return

                    if len(connection.early_data_buffer) < connection.max_early_data_packets:
                        connection.early_data_buffer.append(data)
                        if not connection.connect_requested:
                            connection.connect_requested = True
                            await self._request_to_connect_async(connection)
                        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                            LoggerFactory.get_logger().debug(f'Buffering early data uid: {connection.uid.hex()}, len: {len(data)}, buffer_size: {len(connection.early_data_buffer)}')
                    else:
                        LoggerFactory.get_logger().warning(f'Too much early data buffered, closing connection uid: {connection.uid.hex()}')
                        await self.close_connection_async(connection)
                        return

            # Normal forwarding loop
            while True:
                data = await loop.sock_recv(sock, 65536)

                # 限速
                if limiter and data:
                    wait_time = limiter.acquire(len(data))
                    if wait_time > 0:
                        await asyncio.sleep(wait_time)

                # 发送到 WebSocket
                send_message: MessageEntity = {
                    'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
                    'data': {
                        'name': connection.socket_server.name,
                        'data': data,
                        'uid': connection.uid,
                        'ip_port': connection.socket_server.ip_port
                    }
                }

                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'send to ws uid: {connection.uid.hex()}, len: {len(data)}')

                await handler.write_message(
                    NatSerialization.dumps(send_message, ContextUtils.get_password(),
                                         handler.compress_support, handler.protocol_version),
                    binary=True
                )

                if not data:
                    LoggerFactory.get_logger().info('recv empty, close')
                    break

        except Exception as e:
            LoggerFactory.get_logger().error(f'Forward error uid {connection.uid.hex()}: {e}')
            LoggerFactory.get_logger().error(traceback.format_exc())
        finally:
            await self.close_connection_async(connection)

    async def _request_to_connect_async(self, client_socket_connection: PublicSocketConnection):
        """Request connection to client (async version)"""
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
        await handler.write_message(
            NatSerialization.dumps(send_message, ContextUtils.get_password(),
                                 handler.compress_support, handler.protocol_version),
            binary=True
        )

    def request_to_connect(self, client_socket_connection: PublicSocketConnection):
        """Request connection to client (legacy sync wrapper)"""
        asyncio.ensure_future(self._request_to_connect_async(client_socket_connection))

    async def send_to_socket(self, uid: bytes, message: bytes):
        send_start_time = time.time()
        if uid not in self.uid_to_connection:
            LoggerFactory.get_logger().debug(f'{message}, {uid} not in ')
            return
        if uid not in self.uid_to_send_lock:
            self.uid_to_send_lock[uid] = AsyncioLock()
        async with self.uid_to_send_lock[uid]:
            connection = self.uid_to_connection.get(uid)
            if not connection:
                return
            socket_client = connection.socket
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'send to socket uid: {uid}, len: {len(message)}')
            try:
                await asyncio.wait_for(asyncio.get_event_loop().sock_sendall(socket_client, message), timeout=30)
            except asyncio.TimeoutError:
                LoggerFactory.get_logger().warn(f"Socket send timeout for {uid}, closing connection")
                asyncio.ensure_future(self.close_connection_async(connection))
                return
            except OSError as e:
                LoggerFactory.get_logger().warn(f'{uid} os error: {e}')
                asyncio.ensure_future(self.close_connection_async(connection))
                return
            if not message:
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
            self.uid_to_send_lock.pop(uid, None)
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
