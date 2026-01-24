import asyncio
import json
import logging
import time
import traceback
from asyncio import Lock
from concurrent.futures import ThreadPoolExecutor
from json import JSONDecodeError
from typing import List, Dict, Set, Tuple

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
    protocol_version: int = 1  # Serialization protocol version (1=binary, 2=msgpack)

    # P2P support
    p2p_supported: bool = False  # Whether client supports P2P
    public_ip: str = None        # Client's public IP
    public_port: int = None      # Client's public port (WebSocket connection port)

    client_name_to_handler: Dict[str, 'MyWebSocketaHandler'] = {}
    lock: Lock

    # 客户端到客户端转发路由状态
    # uid → (source_handler, target_handler, rule_name, protocol, target_ip_port)
    c2c_uid_to_routing: Dict[bytes, Tuple['MyWebSocketaHandler', 'MyWebSocketaHandler', str, str, str]] = {}
    c2c_lock: Lock = None

    @classmethod
    def init_locks(cls):
        cls.c2c_lock = asyncio.Lock()

    def open(self, *args: str, **kwargs: str):
        self.lock = Lock()
        self.client_name = None
        self.version = None

        # Get protocol version from URL param, default to 1 for backward compatibility
        try:
            self.protocol_version = int(self.get_argument('v', '1'))
        except (ValueError, TypeError):
            self.protocol_version = 1

        try:
            self.compress_support = json.loads(self.get_argument('c', 'false'))
        except JSONDecodeError:
            self.compress_support = False
        if self.compress_support and not has_snappy:
            msg = 'python-snappy is not installed on the server'
            LoggerFactory.get_logger().info(msg)
            self.close(reason=msg)

        # Record client's public IP and port for P2P
        self.public_ip = self.request.headers.get("X-Real-IP") or self.request.remote_ip
        try:
            nginx_port = self.request.headers.get("X-Remote-Port")
            if nginx_port:
                self.public_port = int(nginx_port)
            else:
                if self.request.connection and self.request.connection.stream and self.request.connection.stream.socket:
                    peer_address = self.request.connection.stream.socket.getpeername()
                    self.public_port = peer_address[1]
                else:
                    self.public_port = 0
        except Exception as e:
            LoggerFactory.get_logger().warning(f'Failed to get client port: {e}')
            self.public_port = 0
        LoggerFactory.get_logger().info(f'New WebSocket connection opened from {self.public_ip}:{self.public_port}, compression: {self.compress_support}, protocol_version: {self.protocol_version}')

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
            message_dict: MessageEntity = NatSerialization.loads(message, ContextUtils.get_password(), self.compress_support, self.protocol_version)
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
            # 智能心跳：记录业务活动（非 PING 消息）
            if self.client_name and message_dict['type_'] != MessageTypeConstant.PING:
                from server.task.heart_beat_task import HeartBeatTask
                heart_beat_task = ContextUtils.get_heart_beat_task()
                if heart_beat_task:
                    heart_beat_task.update_business_activity(self.client_name)

            if message_dict['type_'] == MessageTypeConstant.WEBSOCKET_OVER_TCP:
                data: TcpOverWebsocketMessage = message_dict['data']
                uid = data['uid']

                # 检查是否为 C2C 连接
                is_c2c = uid in self.c2c_uid_to_routing

                # 安全检查：连接所有权校验
                if not is_c2c:
                    conn = tcp_forward_client.uid_to_connection.get(uid)
                    if not conn or conn.socket_server.websocket_handler != self:
                        LoggerFactory.get_logger().debug(f"Security Alert: Client {self.client_name} tried to inject TCP data to UID {uid.hex()} belonging to another client/session.")
                        return

                if is_c2c:
                    source_handler, target_handler, rule_name, protocol, target_ip_port = self.c2c_uid_to_routing[uid]

                    if self == source_handler:
                        if not data.get('ip_port'):
                            data['ip_port'] = target_ip_port
                            message = NatSerialization.dumps(message_dict, ContextUtils.get_password(), target_handler.compress_support, target_handler.protocol_version)
                            await target_handler.write_message(message, binary=True)
                        else:
                            await target_handler.write_message(
                                NatSerialization.dumps(message_dict, ContextUtils.get_password(),
                                                       target_handler.compress_support, target_handler.protocol_version),
                                binary=True
                            )
                    elif self == target_handler:
                        await source_handler.write_message(
                            NatSerialization.dumps(message_dict, ContextUtils.get_password(),
                                                   source_handler.compress_support, source_handler.protocol_version),
                            binary=True
                        )

                    if not data['data']:
                        async with self.c2c_lock:
                            self.c2c_uid_to_routing.pop(uid, None)
                            LoggerFactory.get_logger().info(f'C2C 连接关闭 UID: {uid.hex()}')
                else:
                    await tcp_forward_client.send_to_socket(uid, data['data'])

            elif message_dict['type_'] == MessageTypeConstant.WEBSOCKET_OVER_UDP:
                data: TcpOverWebsocketMessage = message_dict['data']
                uid = data['uid']

                is_c2c = uid in self.c2c_uid_to_routing

                if not is_c2c:
                    is_owned = False
                    udp_client = UdpForwardClient.get_instance()
                    if self.client_name in udp_client.client_name_to_udp_server_set:
                        for server in udp_client.client_name_to_udp_server_set[self.client_name]:
                            if server.get_endpoint_by_uid(uid):
                                is_owned = True
                                break

                    if not is_owned:
                        LoggerFactory.get_logger().warning(f"Security Alert: Client {self.client_name} tried to inject UDP data to UID {uid.hex()} belonging to another client/session.")
                        return

                if is_c2c:
                    source_handler, target_handler, rule_name, protocol, target_ip_port = self.c2c_uid_to_routing[uid]

                    if self == source_handler:
                        if not data.get('ip_port'):
                            data['ip_port'] = target_ip_port
                            message = NatSerialization.dumps(message_dict, ContextUtils.get_password(), target_handler.compress_support, target_handler.protocol_version)
                            await target_handler.write_message(message, binary=True)
                        else:
                            await target_handler.write_message(
                                NatSerialization.dumps(message_dict, ContextUtils.get_password(),
                                                       target_handler.compress_support, target_handler.protocol_version),
                                binary=True
                            )
                    elif self == target_handler:
                        await source_handler.write_message(
                            NatSerialization.dumps(message_dict, ContextUtils.get_password(),
                                                   source_handler.compress_support, source_handler.protocol_version),
                            binary=True
                        )
                else:
                    port = 0
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

            elif message_dict['type_'] == MessageTypeConstant.P2P_PUNCH_REQUEST:
                # Client requests P2P punch with another client
                data = message_dict['data']
                target_client = data.get('target_client')
                await self._handle_punch_request(target_client)

            elif message_dict['type_'] == MessageTypeConstant.PUSH_CONFIG:
                async with self.lock:
                    LoggerFactory.get_logger().info(f'Received push config: {message_dict}')
                    push_config: PushConfigEntity = message_dict['data']
                    client_name = push_config['client_name']
                    self.version = push_config.get('version')

                    # self.p2p_supported = push_config.get('p2p_supported', False)
                    self.p2p_supported = False
                    if self.p2p_supported:
                        LoggerFactory.get_logger().info(f'Client {client_name} supports P2P, public address: {self.public_ip}:{self.public_port}')
                    client_name_to_config_in_server = ContextUtils.get_client_name_to_config_in_server()
                    if client_name in self.client_name_to_handler:
                        self.close(None, 'DuplicatedClientName')
                        raise DuplicatedName()
                    data: List[ClientData] = push_config['config_list']
                    name_in_client = {x['name'] for x in data}
                    if client_name in client_name_to_config_in_server:
                        for config_in_server in client_name_to_config_in_server[client_name]:
                            if config_in_server['name'] in name_in_client:
                                self.close(None, 'DuplicatedNameWithServerConfig')
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
                        else:
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

                    # 推送此客户端相关的 C2C 规则
                    c2c_rules = ContextUtils.get_c2c_rules()
                    client_c2c_rules = []
                    for rule in c2c_rules:
                        if rule.get('source_client') == client_name and rule.get('enabled', True):
                            client_rule = {
                                'name': rule['name'],
                                'target_client': rule['target_client'],
                                'local_port': rule['local_port'],
                                'local_ip': rule['local_ip'],
                                'protocol': rule['protocol'],
                                'speed_limit': rule.get('speed_limit', 0.0),
                                # 'p2p_enabled': rule.get('p2p_enabled', True)
                                'p2p_enabled': False
                            }
                            if 'target_ip' in rule and 'target_port' in rule:
                                client_rule['target_ip'] = rule['target_ip']
                                client_rule['target_port'] = rule['target_port']
                            else:
                                client_rule['target_service'] = rule['target_service']
                            client_c2c_rules.append(client_rule)
                    message_dict['data']['client_to_client_rules'] = client_c2c_rules
                    LoggerFactory.get_logger().info(f'send {len(client_c2c_rules)} C2C to {client_name}')

                    message_dict['data']['public_ip'] = self.public_ip
                    message_dict['data']['public_port'] = self.public_port

                await self.write_message(NatSerialization.dumps(message_dict, key, self.compress_support, self.protocol_version), binary=True)

            elif message_dict['type_'] == MessageTypeConstant.PING:
                self.recv_time = time.time()

            elif message_dict['type_'] == MessageTypeConstant.CONNECT_CONFIRMED:
                data: TcpOverWebsocketMessage = message_dict['data']
                uid = data['uid']

                if uid in self.c2c_uid_to_routing:
                    source_handler, target_handler, rule_name, protocol, _ = self.c2c_uid_to_routing[uid]
                    if self == target_handler:
                        await source_handler.write_message(
                            NatSerialization.dumps(message_dict, ContextUtils.get_password(),
                                                   source_handler.compress_support, source_handler.protocol_version),
                            binary=True
                        )
                elif uid in tcp_forward_client.uid_to_connection:
                    connection = tcp_forward_client.uid_to_connection[uid]
                    connection.connection_confirmed = True

                    for buffered_data in connection.early_data_buffer:
                        if buffered_data:
                            send_message: MessageEntity = {
                                'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
                                'data': {
                                    'name': connection.socket_server.name,
                                    'data': buffered_data,
                                    'uid': uid,
                                    'ip_port': connection.socket_server.ip_port
                                }
                            }
                            await self.write_message(NatSerialization.dumps(send_message, ContextUtils.get_password(), self.compress_support, self.protocol_version), binary=True)
                    connection.early_data_buffer.clear()

            elif message_dict['type_'] == MessageTypeConstant.CONNECT_FAILED:
                data: TcpOverWebsocketMessage = message_dict['data']
                uid = data['uid']
                LoggerFactory.get_logger().info(f'连接失败 uid: {uid}')

                if uid in self.c2c_uid_to_routing:
                    source_handler, target_handler, rule_name, protocol, _ = self.c2c_uid_to_routing[uid]
                    if self == target_handler:
                        await source_handler.write_message(
                            NatSerialization.dumps(message_dict, ContextUtils.get_password(),
                                                   source_handler.compress_support, source_handler.protocol_version),
                            binary=True
                        )
                    async with self.c2c_lock:
                        self.c2c_uid_to_routing.pop(uid, None)
                        LoggerFactory.get_logger().info(f'C2C 连接失败并已清理 UID: {uid.hex()}')
                elif uid in tcp_forward_client.uid_to_connection:
                    connection = tcp_forward_client.uid_to_connection[uid]
                    await tcp_forward_client.close_connection_async(connection)

            # Client-to-client forward request
            elif message_dict['type_'] == MessageTypeConstant.CLIENT_TO_CLIENT_FORWARD:
                data = message_dict['data']
                uid = data['uid']
                target_client = data['target_client']
                source_rule_name = data['source_rule_name']
                protocol = data['protocol']

                c2c_rules = ContextUtils.get_c2c_rules()
                rule = self._find_c2c_rule(source_rule_name, self.client_name, target_client, c2c_rules)
                if not rule or not rule.get('enabled', True):
                    LoggerFactory.get_logger().warn(f'C2C rule does not exist or is disabled: {source_rule_name}')
                    await self._send_connection_failed(uid, source_rule_name)
                    return

                LoggerFactory.get_logger().info(f'C2C rule found: {source_rule_name}, speed_limit: {rule.get("speed_limit", 0.0)} MB/s')

                if target_client not in self.client_name_to_handler:
                    LoggerFactory.get_logger().warn(f'Target client offline: {target_client}')
                    await self._send_connection_failed(uid, source_rule_name)
                    return

                target_handler = self.client_name_to_handler[target_client]

                if 'target_ip' in rule and 'target_port' in rule:
                    target_ip = rule['target_ip']
                    target_port = rule['target_port']
                    LoggerFactory.get_logger().info(f'C2C forward request (direct mode): {self.client_name} → {target_client}/{target_ip}:{target_port} (UID: {uid.hex()})')
                    ip_port = f"{target_ip}:{target_port}"
                    service_name = source_rule_name
                else:
                    target_service = rule.get('target_service')
                    if not target_service:
                        target_service = rule['name']

                    LoggerFactory.get_logger().info(f'C2C forward request (service mode): {self.client_name} → {target_client}/{target_service} (UID: {uid.hex()})')

                    target_service_config = self._find_service_config(target_handler, target_service)
                    if not target_service_config:
                        LoggerFactory.get_logger().warn(f'Target service does not exist: {target_client}/{target_service}')
                        await self._send_connection_failed(uid, source_rule_name)
                        return
                    ip_port = f"{target_service_config['local_ip']}:{target_service_config['local_port']}"
                    service_name = target_service

                async with self.c2c_lock:
                    self.c2c_uid_to_routing[uid] = (self, target_handler, source_rule_name, protocol, ip_port)

                # Try P2P if both clients support it AND rule allows it (only for TCP)
                rule_p2p_enabled = False
                # rule_p2p_enabled = rule.get('p2p_enabled', True)
                if (protocol == 'tcp' and rule_p2p_enabled and
                        self.p2p_supported and target_handler.p2p_supported):
                    # Initiate P2P punch via N4 Signal Service
                    await self._initiate_p2p_punch(target_client)

                # Forward REQUEST_TO_CONNECT to target client (always as fallback)
                message_type = MessageTypeConstant.REQUEST_TO_CONNECT if protocol == 'tcp' else MessageTypeConstant.REQUEST_TO_CONNECT_UDP

                # 获取 C2C 规则中的限速配置
                speed_limit = rule.get('speed_limit', 0.0)

                forward_message: MessageEntity = {
                    'type_': message_type,
                    'data': {
                        'name': service_name,
                        'uid': uid,
                        'ip_port': ip_port,
                        'data': b'',
                        'source_client': self.client_name,  # Add source client info for P2P routing
                        'speed_limit': speed_limit  # 传递限速配置给目标客户端
                    }
                }
                await target_handler.write_message(
                    NatSerialization.dumps(forward_message, ContextUtils.get_password(), target_handler.compress_support, target_handler.protocol_version),
                    binary=True
                )
                LoggerFactory.get_logger().info(f'Connection request forwarded to target client {target_client}, speed_limit: {speed_limit}')

            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'on_message processing took {time.time() - start_time}s')
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    async def _handle_punch_request(self, target_client: str):
        """Handle P2P_PUNCH_REQUEST from client"""
        if not target_client:
            return

        if target_client not in self.client_name_to_handler:
            LoggerFactory.get_logger().warn(f'P2P punch request: target {target_client} offline')
            return

        target_handler = self.client_name_to_handler[target_client]

        if not (self.p2p_supported and target_handler.p2p_supported):
            LoggerFactory.get_logger().info(f'P2P punch skipped: not both clients support P2P')
            return

        await self._initiate_p2p_punch(target_client)

    async def _initiate_p2p_punch(self, target_client: str):
        """Initiate P2P hole punching via N4 Signal Service"""
        n4_service = ContextUtils.get_n4_signal_service()
        if not n4_service:
            LoggerFactory.get_logger().warning('N4 Signal Service not available')
            return

        target_handler = self.client_name_to_handler.get(target_client)
        if not target_handler:
            return

        # Request punch session from N4 Signal Service
        session_id = n4_service.request_punch(self.client_name, target_client)
        session_id_hex = session_id.hex()

        LoggerFactory.get_logger().info(
            f'Initiating P2P punch: {self.client_name} <-> {target_client}, session={session_id_hex}'
        )

        # Send P2P_PUNCH_REQUEST to both clients
        msg_to_self: MessageEntity = {
            'type_': MessageTypeConstant.P2P_PUNCH_REQUEST,
            'data': {
                'session_id': session_id_hex,
                'peer_name': target_client
            }
        }
        await self.write_message(
            NatSerialization.dumps(msg_to_self, ContextUtils.get_password(), self.compress_support, self.protocol_version),
            binary=True
        )

        msg_to_target: MessageEntity = {
            'type_': MessageTypeConstant.P2P_PUNCH_REQUEST,
            'data': {
                'session_id': session_id_hex,
                'peer_name': self.client_name
            }
        }
        await target_handler.write_message(
            NatSerialization.dumps(msg_to_target, ContextUtils.get_password(), target_handler.compress_support, target_handler.protocol_version),
            binary=True
        )

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

                    # 清理 C2C 连接
                    async with self.c2c_lock:
                        uids_to_remove = []
                        for uid, (source_handler, target_handler, rule_name, protocol, _) in list(self.c2c_uid_to_routing.items()):
                            if source_handler == self or target_handler == self:
                                uids_to_remove.append(uid)
                                other_handler = target_handler if source_handler == self else source_handler
                                close_message: MessageEntity = {
                                    'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP if protocol == 'tcp' else MessageTypeConstant.WEBSOCKET_OVER_UDP,
                                    'data': {
                                        'name': rule_name,
                                        'uid': uid,
                                        'data': b'',
                                        'ip_port': ''
                                    }
                                }
                                try:
                                    await other_handler.write_message(
                                        NatSerialization.dumps(close_message, ContextUtils.get_password(),
                                                               other_handler.compress_support, other_handler.protocol_version),
                                        binary=True
                                    )
                                except Exception as e:
                                    LoggerFactory.get_logger().error(f'通知 C2C 对端关闭失败: {e}')

                        for uid in uids_to_remove:
                            self.c2c_uid_to_routing.pop(uid, None)
                            LoggerFactory.get_logger().info(f'已清理 C2C 连接 UID: {uid.hex()}')

            LoggerFactory.get_logger().info(f'Closed {self.client_name} successfully')
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
            raise

    def check_origin(self, origin: str) -> bool:
        return True

    # === C2C 辅助方法 ===

    def _find_c2c_rule(self, rule_name: str, source_client: str, target_client: str, c2c_rules: List[dict]) -> dict:
        """查找并验证 C2C 规则"""
        for rule in c2c_rules:
            if (rule['name'] == rule_name and
                    rule['source_client'] == source_client and
                    rule['target_client'] == target_client):
                return rule
        return None

    def _find_service_config(self, handler: 'MyWebSocketaHandler', service_name: str) -> dict:
        """在客户端的 push_config 中查找服务配置"""
        if not handler.push_config:
            return None
        for config in handler.push_config['config_list']:
            if config['name'] == service_name:
                return config
        return None

    async def _send_connection_failed(self, uid: bytes, rule_name: str):
        """发送连接失败消息给源客户端"""
        fail_message: MessageEntity = {
            'type_': MessageTypeConstant.CONNECT_FAILED,
            'data': {
                'name': rule_name,
                'uid': uid,
                'data': b'',
                'ip_port': ''
            }
        }
        await self.write_message(
            NatSerialization.dumps(fail_message, ContextUtils.get_password(), self.compress_support, self.protocol_version),
            binary=True
        )
