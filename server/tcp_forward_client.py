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
from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class TcpForwardClient:
    _instance = None

    def __init__(self,  loop, tornado_loop):
        self.socket_event_loop =  SelectPool()
        self.uid_to_client: Dict[bytes, socket.socket] = dict()   # uid 对应的连接公网端口的客户端
        self.client_to_uid: Dict[socket.socket, bytes] = dict()  # 连接公网端口的客户端 对应的uid
        self.uid_to_name_ip_port : Dict[bytes, Tuple[str, str]]  = dict()  # uid 对应的内网ipport

        self.uid_to_listen_socket_server: Dict[bytes, socket.socket]  = dict()  # uid对应的监听socket

        self.listen_socket_server_to_name_ip_port: Dict[socket.socket, Tuple[str, str]]  = dict()

        self.listen_socket_server_to_handler: Dict[socket.socket, 'MyWebSocketaHandler']  = dict()

        self.listen_socket_server_to_uid_set: Dict[socket.socket, Set] = defaultdict(set)
        self.client_name_to_listen_socket_server: Dict[str, List[socket.socket]] = defaultdict(list)

        self.tornado_loop = tornado_loop
        self.close_lock = Lock()

        self.client_name_to_lock: Dict[str, AsyncioLock] = dict()
        # self.socket_event_loop.add_callback_function(self.handle_message)

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
            self.listen_socket_server_to_name_ip_port[s] = (name, ip_port)
            self.listen_socket_server_to_handler[s] = websocket_handler
            self.client_name_to_listen_socket_server[client_name].append(s)
            append_data = ResisterAppendData(self.start_accept, SpeedLimiter(speed_limit_size) if speed_limit_size else None)
            self.socket_event_loop.register(s, append_data)

    async def close_by_client_name(self, client_name: str):
        if client_name not in self.client_name_to_listen_socket_server:
            return
        if client_name not in self.client_name_to_lock:
            self.client_name_to_lock[client_name] = AsyncioLock()
        async with self.client_name_to_lock.get(client_name):
            client_socket_list = []
            server_socket_list = []
            uid_list = []

            for server in self.client_name_to_listen_socket_server[client_name]:
                if server in self.listen_socket_server_to_uid_set:
                    for uid in self.listen_socket_server_to_uid_set[server]:
                        uid_list.append(uid)
                        if uid in self.uid_to_client:
                            client_socket_list.append(self.uid_to_client[uid])
                server_socket_list.append(server)
            for c in client_socket_list:
                c.close()
                self.socket_event_loop.unregister(c)
                self.client_to_uid.pop(c)
            for s in server_socket_list:
                self.listen_socket_server_to_name_ip_port.pop(s)
                if s in self.listen_socket_server_to_uid_set:
                    self.listen_socket_server_to_uid_set.pop(s)
                self.listen_socket_server_to_handler.pop(s)
                try:
                    self.socket_event_loop.unregister(s)
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
            for u in uid_list:
                if u in self.uid_to_client:
                    self.uid_to_client.pop(u)
                if u in self.uid_to_listen_socket_server:
                    self.uid_to_listen_socket_server.pop(u)
                if u in self.uid_to_name_ip_port:
                    self.uid_to_name_ip_port.pop(u)
            self.client_name_to_listen_socket_server.pop(client_name)
            self.client_name_to_lock.pop(client_name)
        # self.client_name_to_lock.o

    def handle_message(self, each: socket.socket,  data: ResisterAppendData):
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
        uid = self.client_to_uid[each]
        name, ip_port = self.uid_to_name_ip_port[uid]
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
            'data': {
                'name': name,
                'data': recv,
                'uid': uid,
                'ip_port': ip_port
            }
        }
        s = self.uid_to_listen_socket_server[uid]
        handler = self.listen_socket_server_to_handler[s]
        if not recv:
            LoggerFactory.get_logger().info('recv empty, close')
            try:
                self.close_connection(each)
            except (OSError, ValueError, KeyError):
                LoggerFactory.get_logger().error(f'close error: {traceback.format_exc()}')
        try:

            self.tornado_loop.add_callback(
                partial(handler.write_message, NatSerialization.dumps(send_message, ContextUtils.get_password())), True)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def start_accept(self, s: socket, register_append_data: ResisterAppendData):
        # LoggerFactory.get_logger().info(f'start accept {self.listen_port}')
        # self.socket_event_loop.is_running = True
        # Thread(target=self.socket_event_loop.run).start()
        # while self.is_running:
        #     rs, ws, es = select.select([self.socket], [self.socket], [self.socket])
        #     for each in rs:
        #         if self.socket is None:
        #             continue
        try:
            client, address = s.accept()
        except OSError:
            return
        LoggerFactory.get_logger().info(f'get connect : {address}')
        # 当前 服务端的client 也会对应服务端连接内网服务的一个 client
        uid = os.urandom(4)
        handler = self.listen_socket_server_to_handler[s]
        self.listen_socket_server_to_uid_set[s].add(uid)
        self.uid_to_client[uid] = client
        self.client_to_uid[client] = uid
        self.uid_to_listen_socket_server[uid] = s
        name, ip_port = self.listen_socket_server_to_name_ip_port[s]
        self.uid_to_name_ip_port[uid] = (name, ip_port)
        Thread(target=self.request_to_connect, args=(uid, )).start()
        append_data = ResisterAppendData(self.handle_message, register_append_data.speed_limiter)
        self.socket_event_loop.register(client, append_data)

    def request_to_connect(self, uid: bytes):
        """请求连接客户端"""
        client = self.uid_to_client[uid]
        name, ip_port = self.uid_to_name_ip_port[uid]
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.REQUEST_TO_CONNECT,
            'data': {
                'name': name,
                'data': ''.encode(),
                'uid': uid,
                'ip_port': ip_port
            }
        }
        s = self.uid_to_listen_socket_server[uid]
        handler = self.listen_socket_server_to_handler[s]
        self.tornado_loop.add_callback(
            partial(handler.write_message, NatSerialization.dumps(send_message, ContextUtils.get_password())), True
        )

    async def send_to_socket(self, uid: bytes, message: bytes):
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

        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug(f'send to socket cost time {time.time() - send_start_time}')

    def close_connection(self, socket_client: socket.socket):
        # todo
        LoggerFactory.get_logger().info(f'close {socket_client}')
        # with self.close_lock:
        if socket_client not in self.client_to_uid:
            return
        self.socket_event_loop.unregister(socket_client)
        uid = self.client_to_uid.pop(socket_client)
        self.uid_to_client.pop(uid)
        listen_server_socket = self.uid_to_listen_socket_server.pop(uid)
        self.uid_to_name_ip_port.pop(uid)
        self.listen_socket_server_to_uid_set[listen_server_socket].remove(uid)

        socket_client.close()

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
        self.client_to_uid.clear()
        self.uid_to_client.clear()
        # close server
        # if self.socket:
        #     try:
        #         self.socket.shutdown(socket.SHUT_RDWR)
        #     except OSError:
        #         pass
        #     self.socket.close()
        #     self.socket = None
