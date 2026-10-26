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
from collections import deque
from typing import Dict, Tuple, List, Set, Optional
import os

import tornado

from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.pool import SelectPool, PAUSE_REASON_FLOW_CONTROL, PAUSE_REASON_EARLY_DATA, PAUSE_REASON_WS_BACKPRESSURE
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
        self.early_data_lock = Lock()  # socket 线程缓冲与 tornado 线程 flush 互斥, 防止数据乱序/丢失
        self.early_data_bytes = 0  # early_data_buffer 中的字节数
        self.early_data_flushed = False  # early data 是否已全部写入 websocket, 之前 socket 线程读到的数据都进缓冲, 保证顺序
        self.bytes_from_public: int = 0
        self.bytes_to_public: int = 0
        self.create_time: float = time.time()

    def stats(self) -> str:
        return (f'from_public={self.bytes_from_public} to_public={self.bytes_to_public} '
                f'confirmed={self.connection_confirmed} alive={time.time() - self.create_time:.3f}s')

    def __str__(self):
        return f'{self.uid}_{self.socket}_{self.socket_server.name}'


EARLY_DATA_MAX_BYTES = 1 * 1024 * 1024  # 连接确认前最多缓冲的字节数, 超过后暂停读取该公网连接, 确认并 flush 完成后恢复
SEND_QUEUE_HIGH_WATER = 4 * 1024 * 1024  # 单个 uid 积压超过该值时对来源 websocket 做背压
SEND_QUEUE_LOW_WATER = 1 * 1024 * 1024
# 客户端支持流控时: 超过 FLOW_HIGH_WATER 通知客户端暂停该 uid, 低于 FLOW_LOW_WATER 恢复
# 超过 FLOW_HARD_LIMIT 仍退回 websocket 级背压, 防止对端不响应时内存失控
FLOW_HIGH_WATER = 1 * 1024 * 1024
FLOW_LOW_WATER = 256 * 1024
FLOW_HARD_LIMIT = 8 * 1024 * 1024


class UidSendQueue:
    """单个 uid 的有序发送队列, 只在 tornado 线程中使用"""

    def __init__(self):
        self._items = deque()
        self._event = asyncio.Event()
        self._drain_waiters: List[asyncio.Future] = []
        self.size = 0
        self.closed = False
        self.remote_paused = False  # 是否已通知客户端暂停该 uid

    def put(self, message: bytes):
        if self.closed:
            return
        self._items.append(message)
        self.size += len(message)
        self._event.set()

    async def get(self) -> Optional[bytes]:
        """返回下一条数据, 队列关闭时返回 None"""
        while not self._items:
            if self.closed:
                return None
            self._event.clear()
            await self._event.wait()
        if self.closed:
            return None
        return self._items.popleft()

    def task_done(self, length: int):
        self.size -= length
        if self.size <= SEND_QUEUE_LOW_WATER:
            self._wake_drain_waiters()

    def wait_drained(self) -> asyncio.Future:
        future = asyncio.get_event_loop().create_future()
        self._drain_waiters.append(future)
        return future

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._items.clear()
        self.size = 0
        self._event.set()
        self._wake_drain_waiters()

    def _wake_drain_waiters(self):
        waiters, self._drain_waiters = self._drain_waiters, []
        for future in waiters:
            if not future.done():
                future.set_result(None)


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
        self.uid_to_send_queue: Dict[bytes, 'UidSendQueue'] = dict()  # 每个 uid 一个有序发送队列

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
        recv_error = None
        try:
            recv = each.recv(data.read_size)
        except (BlockingIOError, InterruptedError):
            return  # 非阻塞 socket 的伪就绪, 等下次可读
        except ConnectionResetError as e:
            recv = b''
            recv_error = e
        # client = self.uid_to_client[uid]
        if each not in self.socket_to_connection:
            return
        socket_connection = self.socket_to_connection[each]
        if recv:
            socket_connection.bytes_from_public += len(recv)
        else:
            LoggerFactory.get_logger().info(
                f'public side closed uid: {socket_connection.uid.hex()}, error: {recv_error}, {socket_connection.stats()}')

        # Optimistic send mode: early data 未全部 flush 前, 数据都进缓冲
        if socket_connection.optimistic_mode and not socket_connection.early_data_flushed:
            with socket_connection.early_data_lock:
                # 加锁后再判断一次, 避免与 CONNECT_CONFIRMED 的 flush 竞争
                if not socket_connection.early_data_flushed:
                    if recv:
                        socket_connection.early_data_buffer.append(recv)
                        socket_connection.early_data_bytes += len(recv)
                        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                            LoggerFactory.get_logger().debug(f'Buffering early data uid: {socket_connection.uid}, len: {len(recv)}, buffer_bytes: {socket_connection.early_data_bytes}')
                        if socket_connection.early_data_bytes >= EARLY_DATA_MAX_BYTES:
                            # 不再读取, 数据留在内核缓冲区, flush 完成后恢复
                            self.socket_event_loop.pause_reading(each, PAUSE_REASON_EARLY_DATA)
                        return
                    # 公网侧已关闭, 剩余 early data 没有接收方了, 直接丢弃
                    socket_connection.early_data_buffer = []
                    socket_connection.early_data_bytes = 0
                    if not socket_connection.connection_confirmed:
                        # Client closed connection before confirmation, close immediately
                        LoggerFactory.get_logger().info(f'recv empty before confirmed, close uid: {socket_connection.uid}')
                        try:
                            self.close_connection(socket_connection)
                        except (OSError, ValueError, KeyError):
                            LoggerFactory.get_logger().error(f'Close error: {traceback.format_exc()}')
                        return
                    # 已确认但 flush 未完成: 走下面的正常关闭流程通知客户端

        # --- 发送端限速：暂停读取，延迟恢复 ---
        if data.speed_limiter and recv:
            wait_time = data.speed_limiter.acquire(len(recv))
            if wait_time > 0:
                self.socket_event_loop.pause_and_resume_later(each, wait_time)

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
            message = bytes(NatSerialization.dumps(send_message, ContextUtils.get_password(), is_compress, protocol_version))
            self.tornado_loop.add_callback(self._write_public_data, socket_connection, handler, message)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def _write_public_data(self, connection: PublicSocketConnection, handler: 'MyWebSocketaHandler', message: bytes):
        """
        tornado 线程中把公网数据写入 websocket
        写缓冲积压超过高水位时暂停读取该公网连接, 本次写入 flush 后恢复, 避免大流量占满到客户端的 websocket, 阻塞同一客户端的其他连接
        """
        future = handler.write_bytes(message)
        if future is None or self.uid_to_connection.get(connection.uid) is not connection:
            return
        self.socket_event_loop.pause_reading(connection.socket, PAUSE_REASON_WS_BACKPRESSURE)
        future.add_done_callback(lambda _: self.socket_event_loop.resume_reading(connection.socket, PAUSE_REASON_WS_BACKPRESSURE))

    def start_accept(self, server_socket: socket, register_append_data: ResisterAppendData):
        try:
            client, address = server_socket.accept()
            client: socket.socket
            # 启用 TCP_NODELAY 减少延迟（禁用 Nagle 算法）
            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # 必须非阻塞: loop.sock_sendall 遇到阻塞 socket 会直接阻塞 send, 一个慢连接会卡住整个事件循环
            client.setblocking(False)
        except OSError:
            return
        server = self.listen_socket_to_public_server[server_socket]
        # Current server client also corresponds to a client connecting to internal network service
        uid = os.urandom(4)
        LoggerFactory.get_logger().info(f'Received connection from: {address}, uid: {uid.hex()}, name: {server.name}')
        client_socket_connection = PublicSocketConnection(uid, client, server)
        self.uid_to_connection[uid] = client_socket_connection
        self.socket_to_connection[client] = client_socket_connection
        # accept 后立即请求客户端建立连接, 不能等公网端先发数据: MySQL/VNC/FTP/SMTP 等由服务端先发数据的协议会卡住
        # 早期数据与 CONNECT_CONFIRMED 的竞争由 early_data_lock 保证
        self.request_to_connect(client_socket_connection)
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

    def enqueue_send(self, uid: bytes, message: bytes) -> Optional[asyncio.Future]:
        """
        按 uid 入队发送, 立即返回, 保证同一 uid 内的数据顺序, 不同 uid 之间互不阻塞
        队列积压超过高水位时返回一个 future, 调用方 await 它做背压; 否则返回 None
        """
        connection = self.uid_to_connection.get(uid)
        if not connection:
            LoggerFactory.get_logger().debug(f'{uid.hex()} not in uid_to_connection, drop {len(message)} bytes')
            return None
        send_queue = self.uid_to_send_queue.get(uid)
        if send_queue is None:
            send_queue = UidSendQueue()
            self.uid_to_send_queue[uid] = send_queue
            asyncio.ensure_future(self._send_loop(connection, send_queue))
        send_queue.put(message)
        handler = connection.socket_server.websocket_handler
        if handler.flow_control_support:
            if send_queue.size > FLOW_HIGH_WATER and not send_queue.remote_paused:
                send_queue.remote_paused = True
                handler.send_flow_control(uid, True)
            high_water = FLOW_HARD_LIMIT
        else:
            high_water = SEND_QUEUE_HIGH_WATER
        if send_queue.size > high_water:
            return send_queue.wait_drained()
        return None

    async def _send_loop(self, connection: PublicSocketConnection, send_queue: 'UidSendQueue'):
        uid = connection.uid
        loop = asyncio.get_event_loop()
        try:
            while True:
                message = await send_queue.get()
                if message is None or self.uid_to_connection.get(uid) is not connection:
                    return
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'send to socket uid: {uid.hex()}, len: {len(message)}')
                try:
                    await asyncio.wait_for(loop.sock_sendall(connection.socket, message), timeout=30)
                except asyncio.TimeoutError:
                    LoggerFactory.get_logger().warn(f"Socket send timeout for {uid.hex()}, closing connection")
                    await self.close_connection_async(connection, 'send timeout')
                    return
                except OSError as e:
                    LoggerFactory.get_logger().warn(f'{uid.hex()} os error: {e}')
                    await self.close_connection_async(connection, f'send os error: {e}')
                    return
                connection.bytes_to_public += len(message)
                send_queue.task_done(len(message))
                if send_queue.remote_paused and send_queue.size <= FLOW_LOW_WATER:
                    send_queue.remote_paused = False
                    connection.socket_server.websocket_handler.send_flow_control(uid, False)
                if not message:
                    await self.close_connection_async(connection, 'client reported inner close')
                    return
        finally:
            send_queue.close()
            if self.uid_to_send_queue.get(uid) is send_queue:
                self.uid_to_send_queue.pop(uid, None)

    async def flush_early_data(self, connection: PublicSocketConnection):
        """
        连接确认后分批把 early data 写入 websocket, 每批做背压
        flush 期间公网连接暂停读取, socket 线程已读到的数据继续进缓冲, 全部写完后才切换为直接转发, 保证顺序
        """
        handler = connection.socket_server.websocket_handler
        self.socket_event_loop.pause_reading(connection.socket, PAUSE_REASON_EARLY_DATA)
        try:
            while True:
                with connection.early_data_lock:
                    batch = connection.early_data_buffer
                    connection.early_data_buffer = []
                    connection.early_data_bytes = 0
                    if not batch:
                        connection.early_data_flushed = True
                        return
                for buffered_data in batch:
                    if self.uid_to_connection.get(connection.uid) is not connection:
                        LoggerFactory.get_logger().info(f'connection {connection.uid.hex()} closed, drop remaining early data')
                        return
                    send_message: MessageEntity = {
                        'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
                        'data': {
                            'name': connection.socket_server.name,
                            'data': buffered_data,
                            'uid': connection.uid,
                            'ip_port': connection.socket_server.ip_port
                        }
                    }
                    await handler.wait_backpressure(handler.forward_message(send_message), handler.client_name)
        finally:
            self.socket_event_loop.resume_reading(connection.socket, PAUSE_REASON_EARLY_DATA)

    def set_public_reading_paused(self, connection: PublicSocketConnection, pause: bool):
        """客户端通知暂停/恢复读取公网连接"""
        if pause:
            self.socket_event_loop.pause_reading(connection.socket, PAUSE_REASON_FLOW_CONTROL)
        else:
            self.socket_event_loop.resume_reading(connection.socket, PAUSE_REASON_FLOW_CONTROL)

    async def close_connection_async(self, connection: PublicSocketConnection, reason: str = ''):
        """Asynchronously close connection to avoid blocking in event loop"""
        try:
            LoggerFactory.get_logger().info(
                f'Async closing connection {connection.uid.hex()}, reason: {reason}, {connection.stats()}')
            uid = connection.uid
            if uid not in self.uid_to_connection:
                return
            # Remove from tracking dictionaries
            self.uid_to_connection.pop(uid, None)
            self.socket_to_connection.pop(connection.socket, None)
            send_queue = self.uid_to_send_queue.pop(uid, None)
            if send_queue:
                send_queue.close()
            connection.socket_server.delete_client(connection)
            # Ensure unregister before closing
            try:
                self.socket_event_loop.unregister(connection.socket)
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
            LoggerFactory.get_logger().info(f'Closing connection {connection.uid.hex()}, {connection.stats()}')
            uid = connection.uid
            if uid not in self.uid_to_connection:
                return
            self.socket_event_loop.unregister(connection.socket)
            self.uid_to_connection.pop(uid, None)
            self.socket_to_connection.pop(connection.socket, None)
            send_queue = self.uid_to_send_queue.pop(uid, None)
            if send_queue:
                # 可能在 socket 线程调用, 交给 tornado 线程关闭队列
                self.tornado_loop.add_callback(send_queue.close)
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
        s.listen(256)
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
