import asyncio
import json
import logging
import time
import traceback
from asyncio import Lock
from concurrent.futures import ThreadPoolExecutor
from json import JSONDecodeError
from typing import List, Dict, Set

from server.udp_forward_client import UdpForwardClient

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

    compress_support: bool = False  # Whether snappy compression is supported

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
        LoggerFactory.get_logger().info(f'New WebSocket connection opened, compression supported: {self.compress_support}')

    async def write_message(self, message, binary=False):
        start_time = time.time()
        try:
            byte_message = bytes(message)
            await (super(MyWebSocketaHandler, self).write_message(byte_message, binary))
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'Write message cost {time.time() - start_time}s, length: {len(message)}')
            return
        except Exception:
            LoggerFactory.get_logger().info(f'Send error: {message[:10]}')
            LoggerFactory.get_logger().error(traceback.format_exc())
            raise

    def on_message(self, m_bytes):
        asyncio.ensure_future(self.on_message_async(m_bytes))

    async def on_message_async(self, message):
        tcp_forward_client = TcpForwardClient.get_instance()
        try:
            message_dict: MessageEntity = NatSerialization.loads(message, ContextUtils.get_password(), self.compress_support)
        except json.decoder.JSONDecodeError:
            self.close(reason='Invalid password')
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
                data: TcpOverWebsocketMessage = message_dict['data']  # TCP message
                uid = data['uid']
                await tcp_forward_client.send_to_socket(uid, data['data'])
            elif message_dict['type_'] == MessageTypeConstant.WEBSOCKET_OVER_UDP:
                data: TcpOverWebsocketMessage = message_dict['data']  # UDP message
                uid = data['uid']
                port = 0

                # Find corresponding UDP port
                for p, srv in list(UdpForwardClient.get_instance().udp_servers.items()):
                    for endpoint_uid, endpoint in srv.uid_to_endpoint.items():
                        if endpoint_uid == uid:
                            port = p
                            break
                    if port != 0:
                        break

                if port != 0:
                    await UdpForwardClient.get_instance().send_udp(uid, data['data'], port)
                else:
                    LoggerFactory.get_logger().warning(f"Could not find UDP port for UID {uid}")
            elif message_dict['type_'] == MessageTypeConstant.PUSH_CONFIG:
                async with self.lock:
                    LoggerFactory.get_logger().info(f'Received push config: {message_dict}')
                    push_config: PushConfigEntity = message_dict['data']
                    client_name = push_config['client_name']
                    self.version = push_config.get('version')
                    client_name_to_config_in_server = ContextUtils.get_client_name_to_config_in_server()
                    if client_name in self.client_name_to_handler:
                        self.close(None, 'DuplicatedClientName')  # Duplicated client name on server
                        raise DuplicatedName()
                    data: List[ClientData] = push_config['config_list']
                    name_in_client = {x['name'] for x in data}
                    if client_name in client_name_to_config_in_server:
                        for config_in_server in client_name_to_config_in_server[client_name]:
                            if config_in_server['name'] in name_in_client:
                                self.close(None, 'DuplicatedNameWithServerConfig')  # Name conflicts with server config
                                raise DuplicatedName()
                        data.extend(client_name_to_config_in_server[client_name])
                    key = push_config['key']
                    if key != ContextUtils.get_password():
                        self.close(reason='Invalid password')
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
                    udp_servers_list = []
                    for d in data:
                        protocol = d.get('protocol', 'tcp')
                        d.setdefault('protocol', protocol)
                        if protocol.lower() == 'tcp':
                            try:
                                listen_socket = tcp_forward_client.create_listen_socket(d['remote_port'])
                            except OSError:
                                self.close(None, 'Address already in use')
                                raise
                            ip_port = d['local_ip'] + ':' + str(d['local_port'])
                            d.setdefault('speed_limit', 0)
                            speed_limit: float = d.get('speed_limit', 0)
                            await tcp_forward_client.register_listen_server(listen_socket, d['name'], ip_port, self, speed_limit)
                            listen_socket_list.append(listen_socket)
                        else:
                            # UDP processing
                            ip_port = d['local_ip'] + ':' + str(d['local_port'])
                            d.setdefault('speed_limit', 0)
                            speed_limit: float = d.get('speed_limit', 0)
                            success = await UdpForwardClient.get_instance().register_udp_server(
                                d['remote_port'], d['name'], ip_port, self, speed_limit)
                            if not success:
                                self.close(None, f'UDP port {d["remote_port"]} already in use or registration failed')
                                raise Exception(f"UDP port {d['remote_port']} already in use or registration failed")
                            LoggerFactory.get_logger().info(f"UDP service registered: {d['name']} on port {d['remote_port']}")
                    self.recv_time = time.time()
                    self.client_name_to_handler[client_name] = self
                    self.push_config = push_config
                await self.write_message(NatSerialization.dumps(message_dict, key, self.compress_support), binary=True)
            elif message_dict['type_'] == MessageTypeConstant.PING:
                self.recv_time = time.time()
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'on_message processing took {time.time() - start_time}s')
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def on_close(self, code: int = None, reason: str = None) -> None:
        asyncio.ensure_future(self._on_close(code, reason))

    async def _on_close(self, code: int = None, reason: str = None) -> None:
        LoggerFactory.get_logger().info(f'Closing connection {self.client_name}, code: {code}, reason: {reason}')
        try:
            async with self.lock:
                if self.client_name:
                    if self.client_name in self.client_name_to_handler:
                        self.client_name_to_handler.pop(self.client_name)
                    await TcpForwardClient.get_instance().close_by_client_name(self.client_name)
                    await UdpForwardClient.get_instance().close_by_client_name(self.client_name)
            LoggerFactory.get_logger().info(f'Closed {self.client_name} successfully')
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
            raise

    def check_origin(self, origin: str) -> bool:
        return True
