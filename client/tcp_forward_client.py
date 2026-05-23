import logging
import os
import socket
import threading
import time
import traceback
from threading import Lock
from typing import Dict, List

from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.pool import SelectPool
from common.register_append_data import ResisterAppendData
from common.speed_limit import SpeedLimiter
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class PrivateSocketConnection:
    """Client connecting to internal network port"""

    def __init__(self, uid: bytes, s: socket.socket, name: str):
        self.uid: bytes = uid
        self.socket: socket.socket = s
        self.name: str = name


class TcpForwardClient:
    def __init__(self, ws_sender, compress_support: bool, protocol_version: int):
        self.uid_to_socket_connection: Dict[bytes, PrivateSocketConnection] = dict()
        self.socket_to_socket_connection: Dict[socket.socket, PrivateSocketConnection] = dict()
        self.compress_support: bool = compress_support
        self.protocol_version: int = protocol_version
        self.ws = ws_sender  # WsSender — thread-safe, send via IOLoop.add_callback
        self.lock = Lock()
        self.socket_event_loop = SelectPool()

        # C2C client-to-client forward state
        self.c2c_rules: List[dict] = []
        self.c2c_listeners: Dict[str, socket.socket] = {}
        self.c2c_uid_to_rule: Dict[bytes, str] = {}

        # N4 Tunnel Manager for P2P data transfer
        self.tunnel_manager = None

        # Pending data buffer: uid -> list of (data, timestamp)
        self.pending_data: Dict[bytes, list] = {}
        self.pending_data_lock = Lock()
        self.pending_data_timeout = 10

    def set_running(self, running: bool):
        self.socket_event_loop.is_running = running

    def update_websocket(self, ws):
        """No-op: ws_sender reference is stable; connection is updated inside WsSender."""
        pass

    def start_forward(self):
        self.socket_event_loop.run()

    def setup_c2c_tcp_listeners(self, c2c_rules: List[dict]):
        # Clean old listeners first
        for rule_name, listener in list(self.c2c_listeners.items()):
            try:
                listener.close()
                LoggerFactory.get_logger().info('Cleaned old C2C TCP listener: %s' % rule_name)
            except Exception as e:
                LoggerFactory.get_logger().error('Failed to close old C2C TCP listener %s: %s' % (rule_name, e))

        self.c2c_listeners.clear()
        self.c2c_uid_to_rule.clear()

        self.c2c_rules = c2c_rules
        LoggerFactory.get_logger().info('Setting up %d C2C TCP listeners' % len(c2c_rules))

        for rule in c2c_rules:
            if rule['protocol'] != 'tcp':
                continue

            rule_name = rule['name']
            local_ip = rule['local_ip']
            local_port = rule['local_port']

            try:
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((local_ip, local_port))
                listener.listen(128)

                self.c2c_listeners[rule_name] = listener
                LoggerFactory.get_logger().info('C2C TCP listener created: %s on %s:%d' % (rule_name, local_ip, local_port))

                accept_thread = threading.Thread(
                    target=self.handle_c2c_tcp_accept,
                    args=(listener, rule),
                    daemon=True
                )
                accept_thread.start()

            except Exception as e:
                LoggerFactory.get_logger().error('Failed to create C2C TCP listener %s: %s' % (rule_name, e))
                LoggerFactory.get_logger().error(traceback.format_exc())

    def handle_c2c_tcp_accept(self, listener: socket.socket, rule: dict):
        rule_name = rule['name']
        target_client = rule['target_client']
        protocol = rule['protocol']
        speed_limit = rule.get('speed_limit', 0.0)

        use_direct_mode = 'target_ip' in rule and 'target_port' in rule

        LoggerFactory.get_logger().info('C2C TCP accept thread started: %s' % rule_name)

        while True:
            try:
                client_socket, client_addr = listener.accept()
                LoggerFactory.get_logger().info('C2C TCP connection accepted: %s from %s' % (rule_name, client_addr))

                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                uid = os.urandom(4)

                with self.lock:
                    self.c2c_uid_to_rule[uid] = rule_name
                    connection = PrivateSocketConnection(uid, client_socket, rule_name)
                    self.uid_to_socket_connection[uid] = connection
                    self.socket_to_socket_connection[client_socket] = connection

                if self.tunnel_manager:
                    self.tunnel_manager.register_uid(uid, target_client)
                    LoggerFactory.get_logger().info('Registered UID %s to peer %s' % (uid.hex(), target_client))

                forward_data = {
                    'uid': uid,
                    'target_client': target_client,
                    'source_rule_name': rule_name,
                    'protocol': protocol
                }

                if use_direct_mode:
                    forward_data['target_ip'] = rule['target_ip']
                    forward_data['target_port'] = rule['target_port']
                else:
                    forward_data['target_service'] = rule['target_service']

                forward_message: MessageEntity = {
                    'type_': MessageTypeConstant.CLIENT_TO_CLIENT_FORWARD,
                    'data': forward_data
                }
                self.ws.send(
                    NatSerialization.dumps(forward_message, ContextUtils.get_password(), self.compress_support, self.protocol_version)
                )
                LoggerFactory.get_logger().info('C2C forward request sent: %s UID: %s' % (rule_name, uid.hex()))

                speed_limiter = SpeedLimiter(speed_limit) if speed_limit > 0 else None
                self.socket_event_loop.register(client_socket, ResisterAppendData(self.handle_message, speed_limiter))

            except OSError:
                LoggerFactory.get_logger().info('C2C TCP listener closed: %s' % rule_name)
                break
            except Exception as e:
                LoggerFactory.get_logger().error('C2C TCP accept connection error %s: %s' % (rule_name, e))
                LoggerFactory.get_logger().error(traceback.format_exc())

    def handle_message(self, each: socket.socket, data: ResisterAppendData):
        connection = self.socket_to_socket_connection.get(each)
        if not connection:
            return

        try:
            recv = each.recv(data.read_size)
        except OSError:
            recv = b''

        if data.speed_limiter and recv:
            wait_time = data.speed_limiter.acquire(len(recv))
            if wait_time > 0:
                self.socket_event_loop.pause_and_resume_later(each, wait_time)

        if self.tunnel_manager:
            if self.tunnel_manager.send_data(connection.uid, recv):
                if not recv:
                    self.close_connection(each)
                return

        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
            'data': {
                'name': connection.name,
                'data': recv,
                'uid': connection.uid,
                'ip_port': ''
            }
        }

        self.ws.send(
            NatSerialization.dumps(send_message, ContextUtils.get_password(), self.compress_support, self.protocol_version)
        )

        if not recv:
            try:
                self.close_connection(each)
            except Exception:
                pass

    def create_socket(self, name: str, uid: bytes, ip_port: str, speed_limiter: SpeedLimiter) -> bool:
        if uid in self.uid_to_socket_connection:
            return True
        connection = None
        with self.lock:
            if uid in self.uid_to_socket_connection:
                return True
            try:
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug('create socket %s, %s' % (name, uid))

                if not ip_port or ':' not in ip_port:
                    LoggerFactory.get_logger().error('Invalid ip_port: %r, name: %s, uid: %s' % (ip_port, name, uid.hex()))
                    return False

                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                connection = PrivateSocketConnection(uid, s, name)
                self.socket_to_socket_connection[s] = connection
                ip, port = ip_port.split(':')
                try:
                    s.connect((ip, int(port)))

                    confirm_message: MessageEntity = {
                        'type_': MessageTypeConstant.CONNECT_CONFIRMED,
                        'data': {
                            'name': name,
                            'data': b'',
                            'uid': uid,
                            'ip_port': ip_port
                        }
                    }
                    self.ws.send(
                        NatSerialization.dumps(confirm_message, ContextUtils.get_password(), self.compress_support, self.protocol_version)
                    )
                    if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                        LoggerFactory.get_logger().debug('Connection confirmation message sent uid: %s' % uid)

                except OSError as e:
                    LoggerFactory.get_logger().info('connection error, %s' % e)

                    fail_message: MessageEntity = {
                        'type_': MessageTypeConstant.CONNECT_FAILED,
                        'data': {
                            'name': name,
                            'data': b'',
                            'uid': uid,
                            'ip_port': ip_port
                        }
                    }
                    try:
                        self.ws.send(
                            NatSerialization.dumps(fail_message, ContextUtils.get_password(), self.compress_support, self.protocol_version)
                        )
                    except Exception as send_err:
                        LoggerFactory.get_logger().error('Failed to send connection failure message: %s' % send_err)

                    self.close_connection(s)
                    self.close_remote_socket(connection)
                    return False

                self.uid_to_socket_connection[uid] = connection
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug('register socket %s, %s' % (name, uid))
                self.socket_event_loop.register(s, ResisterAppendData(self.handle_message, speed_limiter))
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug('register socket success %s, %s' % (name, uid))

                self._flush_pending_data(uid, s)

                return True
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
                if connection:
                    self.close_remote_socket(connection)
                return False

    def close_connection(self, socket_client: socket.socket):
        LoggerFactory.get_logger().info('Closing socket %s' % socket_client)
        if socket_client in self.socket_to_socket_connection:
            connection: PrivateSocketConnection = self.socket_to_socket_connection.pop(socket_client)
            self.socket_event_loop.unregister(socket_client)
            try:
                socket_client.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                LoggerFactory.get_logger().warn('Shutdown OS error %s' % e)
            socket_client.close()
            LoggerFactory.get_logger().info('Socket closed successfully %s' % socket_client)
            if connection.uid in self.uid_to_socket_connection:
                self.uid_to_socket_connection.pop(connection.uid)
            self.close_remote_socket(connection)

    def close(self):
        LoggerFactory.get_logger().info('Starting to close %s' % self.c2c_listeners)
        with self.lock:
            for rule_name, listener in self.c2c_listeners.items():
                try:
                    try:
                        listener.shutdown(socket.SHUT_RDWR)
                    except OSError as e:
                        LoggerFactory.get_logger().warn('Shutdown OS error %s' % e)
                    listener.close()
                    LoggerFactory.get_logger().info('C2C TCP listener closed: %s' % rule_name)
                except Exception as e:
                    LoggerFactory.get_logger().error('Failed to close C2C TCP listener %s: %s' % (rule_name, e))

            self.c2c_listeners.clear()
            self.c2c_rules.clear()
            self.c2c_uid_to_rule.clear()

            self.socket_event_loop.stop()
            for uid, c in self.uid_to_socket_connection.items():
                s = c.socket
                try:
                    self.socket_event_loop.unregister(s)
                except Exception:
                    LoggerFactory.get_logger().error(traceback.format_exc())
                try:
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError as e:
                        LoggerFactory.get_logger().warn('Shutdown OS error %s' % e)
                    s.close()
                except Exception:
                    LoggerFactory.get_logger().error(traceback.format_exc())
            self.uid_to_socket_connection.clear()
            self.socket_to_socket_connection.clear()
            self.set_running(False)
            self.socket_event_loop.clear()

    def close_remote_socket(self, connection: PrivateSocketConnection):
        name = connection.name
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
            'data': {
                'name': name,
                'data': b'',
                'uid': connection.uid,
                'ip_port': ''
            }
        }
        start_time = time.time()
        self.ws.send(
            NatSerialization.dumps(send_message, ContextUtils.get_password(), self.compress_support, self.protocol_version)
        )
        LoggerFactory.get_logger().debug('Send to websocket cost time %s' % (time.time() - start_time))

    def send_by_uid(self, uid: bytes, msg: bytes):
        connection = self.uid_to_socket_connection.get(uid)
        if not connection:
            if msg:
                with self.pending_data_lock:
                    if uid not in self.pending_data:
                        self.pending_data[uid] = []
                    self.pending_data[uid].append((msg, time.time()))
                    LoggerFactory.get_logger().debug(
                        'Buffered %d bytes for UID %s (connection pending)' % (len(msg), uid.hex())
                    )
            return
        try:
            s = connection.socket
            s.settimeout(30)
            s.sendall(msg)
            s.settimeout(None)
            if not msg:
                self.close_connection(s)
        except socket.timeout:
            LoggerFactory.get_logger().warning('sendall timeout, closing uid: %s' % connection.uid.hex())
            self.close_connection(s)
            self.close_remote_socket(connection)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
            self.close_remote_socket(connection)

    def _flush_pending_data(self, uid: bytes, sock: socket.socket):
        pending_items = None
        with self.pending_data_lock:
            if uid in self.pending_data:
                pending_items = self.pending_data.pop(uid)

        if not pending_items:
            return

        now = time.time()
        total_sent = 0
        expired_count = 0

        for data, timestamp in pending_items:
            if now - timestamp > self.pending_data_timeout:
                expired_count += 1
                continue
            try:
                sock.sendall(data)
                total_sent += len(data)
            except Exception as e:
                LoggerFactory.get_logger().error('Failed to flush pending data for UID %s: %s' % (uid.hex(), e))
                break

        if total_sent > 0 or expired_count > 0:
            LoggerFactory.get_logger().info(
                'Flushed pending data for UID %s: %d bytes sent, %d expired packets dropped'
                % (uid.hex(), total_sent, expired_count)
            )
