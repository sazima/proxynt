import asyncio
import json
import time
import traceback
from asyncio import Lock
from collections import defaultdict
from threading import Thread
from typing import List, Dict, Set

from tornado.ioloop import IOLoop
from tornado.websocket import WebSocketHandler

from common.nat_serialization import NatSerialization
from common.logger_factory import LoggerFactory
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity
from entity.message.push_config_entity import PushConfigEntity, ClientData
from entity.message.tcp_over_websocket_message import TcpOverWebsocketMessage
from exceptions.duplicated_name import DuplicatedName
from exceptions.invalid_password import InvalidPassword
from server.tcp_forward_client import TcpForwardClient


class MyWebSocketaHandler(WebSocketHandler):
    client_name: str
    push_config: PushConfigEntity

    name_to_tcp_forward_client: Dict[str, TcpForwardClient] = {}
    handler_to_names: Dict['MyWebSocketaHandler', Set[str]] = defaultdict(set)
    handler_to_recv_time: Dict['MyWebSocketaHandler', float] = {}
    client_name_to_handler: Dict[str, 'MyWebSocketaHandler'] = {}
    lock = Lock()

    def _check_password(self, request_password: str) -> bool:
        password = ContextUtils.get_password()
        if not password and not request_password:
            return True
        return password == request_password

    def open(self, *args: str, **kwargs: str):
        self.client_name = None
        LoggerFactory.get_logger().info('new open websocket')

    async def write_message(self, message, binary=False):
        start_time = time.time()
        try:
            await (super(MyWebSocketaHandler, self).write_message(bytes(message), binary))
            LoggerFactory.get_logger().debug(f'write message cost time {time.time() - start_time}')
            return
        except Exception:
            LoggerFactory.get_logger().info(message)
            LoggerFactory.get_logger().error(traceback.format_exc())
            raise

    def on_message(self, m_bytes):
        asyncio.ensure_future(self.on_message_async(m_bytes))

    async def on_message_async(self, message):
        try:
            message_dict: MessageEntity = NatSerialization.loads(message, ContextUtils.get_password())
        except json.decoder.JSONDecodeError:
            self.close(reason='invalid password')
            raise InvalidPassword()
        try:
            start_time = time.time()
            if message_dict['type_'] == MessageTypeConstant.WEBSOCKET_OVER_TCP:
                data: TcpOverWebsocketMessage = message_dict['data']  # socket消息
                name = data['name']
                uid = data['uid']
                await self.name_to_tcp_forward_client[name].send_to_socket(uid, data['data'])
            elif message_dict['type_'] == MessageTypeConstant.PUSH_CONFIG:
                async with self.lock:
                    LoggerFactory.get_logger().info(f'get push config: {message_dict}')
                    push_config: PushConfigEntity = message_dict['data']
                    client_name = push_config['client_name']
                    client_name_to_config_in_server = ContextUtils.get_client_name_to_config_in_server()
                    if client_name in self.client_name_to_handler:
                        self.close(None, 'DuplicatedClientName')  # 与服务器上配置的名字重复
                        raise DuplicatedName()
                        pass
                    # todo 获取服务端配置
                    data: List[ClientData] = push_config['config_list']  # 配置
                    name_in_client = {x['name'] for x in data}
                    if client_name in client_name_to_config_in_server:
                        for config_in_server in client_name_to_config_in_server[client_name]:
                            if config_in_server['name'] in name_in_client:
                                self.close(None, 'DuplicatedNameWithServerConfig')  # 与服务器上配置的名字重复
                                raise DuplicatedName()
                        data.extend(client_name_to_config_in_server[client_name])
                    key = push_config['key']
                    if key != ContextUtils.get_password():
                        self.close(reason='invalid password')
                        raise InvalidPassword()
                    this_name_to_tcp_forward_client = {}
                    name_set = set()
                    for d in data:
                        if d['name'] in self.name_to_tcp_forward_client:
                            self.close(None, 'DuplicatedName')
                            raise DuplicatedName()
                        if d['name'] in name_set:
                            self.close(None, 'DuplicatedName')
                            raise DuplicatedName()
                        ip_port = d['local_ip'] + ':' + str(d['local_port'])
                        client = TcpForwardClient(self, d['name'], d['remote_port'], asyncio.get_event_loop(),
                                                  IOLoop.current(), ip_port)
                        this_name_to_tcp_forward_client[d['name']] = client
                        name_set.add(d['name'])
                    task_list: List[Thread] = []
                    for name, client in this_name_to_tcp_forward_client.items():
                        try:
                            client.bind_port()
                        except OSError:
                            for _, client in this_name_to_tcp_forward_client.items():
                                client.close()
                            raise
                        task_list.append(Thread(target=client.start_accept))
                    self.name_to_tcp_forward_client.update(this_name_to_tcp_forward_client)
                    for name, _ in this_name_to_tcp_forward_client.items():
                        self.handler_to_names[self].add(name)
                    for t in task_list:
                        t.start()

                    self.handler_to_recv_time[self] = time.time()
                    self.client_name = client_name
                    self.client_name_to_handler[client_name] = self
                    self.push_config = push_config
            elif message_dict['type_'] == MessageTypeConstant.PING:
                self.handler_to_recv_time[self] = time.time()
            LoggerFactory.get_logger().debug(f'on message cost time {time.time() - start_time}')
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def on_close(self, code: int = None, reason: str = None) -> None:
        asyncio.ensure_future(self._on_close(code, reason))

    async def _on_close(self, code: int = None, reason: str = None) -> None:
        try:
            async with self.lock:
                LoggerFactory.get_logger().info('close')
                names = self.handler_to_names[self]
                for name in names:
                    try:
                        client = self.name_to_tcp_forward_client.pop(name)
                        client.close()
                    except KeyError:
                        pass
                self.handler_to_names.pop(self)
                if self in self.handler_to_recv_time:
                    self.handler_to_recv_time.pop(self)
                if self.client_name in self.client_name_to_handler:
                    self.client_name_to_handler.pop(self.client_name)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
            raise


    def check_origin(self, origin: str) -> bool:
        return True
