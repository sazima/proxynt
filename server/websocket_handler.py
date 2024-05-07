import asyncio
import json
import logging
import time
import traceback
from asyncio import Lock
from concurrent.futures import ThreadPoolExecutor
from json import JSONDecodeError
from typing import List, Dict, Set

try:
    import snappy
    has_snappy = True
except ModuleNotFoundError:
    has_snappy = False

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
from exceptions.replay_error import ReplayError
from exceptions.signature_error import SignatureError
from server.tcp_forward_client import TcpForwardClient

p = ThreadPoolExecutor(max_workers=100)


class MyWebSocketaHandler(WebSocketHandler):
    client_name: str
    version: str
    push_config: PushConfigEntity
    names: Set[str]
    recv_time: float = None

    compress_support: bool = False  # 是否支持snappy压缩

    # handler_to_recv_time: Dict['MyWebSocketaHandler', float] = {}
    client_name_to_handler: Dict[str, 'MyWebSocketaHandler'] = {}
    lock: Lock

    def open(self, *args: str, **kwargs: str):
        self.lock = Lock()
        self.client_name = None
        self.version = None
        try:
            self.compress_support = json.loads(self.get_argument('c', 'false'))
        except JSONDecodeError:
            self.compress_support = False
        if self.compress_support and not has_snappy:
            msg = 'python-snappy is not installed on the server'
            LoggerFactory.get_logger().info(msg)
            self.close(reason=msg)
        LoggerFactory.get_logger().info(f'new open websocket, compress_support: {self.compress_support}')

    async def write_message(self, message, binary=False):
        start_time = time.time()
        try:
            byte_message = bytes(message)
            # if self.compress_support:
            #     byte_message = await asyncio.get_event_loop().run_in_executor(p, snappy.snappy.compress, byte_message)
            await (super(MyWebSocketaHandler, self).write_message(byte_message, binary))
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'write message cost time {time.time() - start_time}, len: {len(message)}')
            return
        except Exception:
            LoggerFactory.get_logger().info(f'send error: {message[:10]}')
            LoggerFactory.get_logger().error(traceback.format_exc())
            raise

    def on_message(self, m_bytes):
        asyncio.ensure_future(self.on_message_async(m_bytes))

    async def on_message_async(self, message):
        # old_len = len(message)
        # message = await asyncio.get_event_loop().run_in_executor(p, snappy.snappy.uncompress, message)
        # if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
        #     LoggerFactory.get_logger().info(f'压缩了: {len(message) - old_len }::: {len(message)}, {old_len}')
        tcp_forward_client = TcpForwardClient.get_instance()
        try:
            message_dict: MessageEntity = NatSerialization.loads(message, ContextUtils.get_password(), self.compress_support)
        except json.decoder.JSONDecodeError:
            self.close(reason='invalid password')
            raise InvalidPassword()
        except SignatureError:
            self.close(reason='SignatureError')
            raise
        except ReplayError:
            self.close(reason='ReplayError')
            raise
        try:
            start_time = time.time()
            if message_dict['type_'] == MessageTypeConstant.WEBSOCKET_OVER_TCP:
                data: TcpOverWebsocketMessage = message_dict['data']  # socket消息
                uid = data['uid']
                await tcp_forward_client.send_to_socket(uid, data['data'])
            elif message_dict['type_'] == MessageTypeConstant.PUSH_CONFIG:
                async with self.lock:
                    LoggerFactory.get_logger().info(f'get push config: {message_dict}')
                    push_config: PushConfigEntity = message_dict['data']
                    client_name = push_config['client_name']
                    self.version = push_config.get('version')
                    client_name_to_config_in_server = ContextUtils.get_client_name_to_config_in_server()
                    if client_name in self.client_name_to_handler:
                        self.close(None, 'DuplicatedClientName')  # 与服务器上配置的名字重复
                        raise DuplicatedName()
                        pass
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

                    name_set = set()
                    for d in data:
                        if d['name'] in name_set:
                            self.close(None, 'DuplicatedName')
                            raise DuplicatedName()
                        name_set.add(d['name'])
                    self.client_name = client_name
                    self.names = name_set
                    listen_socket_list = []
                    for d in data:
                        try:
                            listen_socket = tcp_forward_client.create_listen_socket(d['remote_port'])
                        except OSError:
                            self.close(None, 'Address already in use')
                            # tcp_forward_client.close_by_client_name(self.client_name)
                            raise
                        ip_port = d['local_ip'] + ':' + str(d['local_port'])
                        d.setdefault('speed_limit', 0)
                        speed_limit: float = d.get('speed_limit', 0)  # 网速限制
                        await tcp_forward_client.register_listen_server(listen_socket, d['name'], ip_port, self, speed_limit)
                        listen_socket_list.append(listen_socket)
                    # self.handler_to_recv_time[self] = time.time()
                    self.recv_time = time.time()
                    self.client_name_to_handler[client_name] = self
                    self.push_config = push_config
                await self.write_message(NatSerialization.dumps(message_dict, key, self.compress_support), binary=True)  # 更新完配置再发给客户端
            elif message_dict['type_'] == MessageTypeConstant.PING:
                self.recv_time = time.time()
                # if self.client_name:  # 只有带 client_name 的心跳时间才有用
                #     self.handler_to_recv_time[self] = time.time()
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'on message cost time {time.time() - start_time}')
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def on_close(self, code: int = None, reason: str = None) -> None:
        asyncio.ensure_future(self._on_close(code, reason))

    async def _on_close(self, code: int = None, reason: str = None) -> None:
        LoggerFactory.get_logger().info(f'close {self.client_name}, code: {code}, reason, {reason}')
        try:
            async with self.lock:
                if self.client_name:
                    if self.client_name in self.client_name_to_handler:
                        self.client_name_to_handler.pop(self.client_name)
                    await TcpForwardClient.get_instance().close_by_client_name(self.client_name)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
            raise

    def check_origin(self, origin: str) -> bool:
        return True
