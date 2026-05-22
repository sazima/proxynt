import logging
import os
import socket
import time
import traceback
import threading
from typing import Dict, List
from threading import Thread

from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.speed_limit import SpeedLimiter
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class UdpSocketConnection:
    """Client connecting to internal UDP port"""
    def __init__(self, uid: bytes, socket_obj: socket.socket, name: str, ip_port: str, ws_sender, speed_limiter=None):
        self.uid = uid
        self.socket = socket_obj
        self.name = name
        self.ip_port = ip_port
        self.ws = ws_sender
        self.speed_limiter = speed_limiter
        self.target_address = None
        self.last_active_time = time.time()

        if ":" in ip_port:
            ip, port_str = ip_port.split(":")
            self.target_address = (ip, int(port_str))
        else:
            self.target_address = None


class UdpForwardClient:
    """UDP forward client"""
    def __init__(self, ws_sender, compress_support: bool, protocol_version: int):
        self.uid_to_connection: Dict[bytes, UdpSocketConnection] = {}
        self.ws = ws_sender  # WsSender — thread-safe
        self.compress_support = compress_support
        self.protocol_version = protocol_version
        self.running = True
        self.lock = threading.Lock()

        self.receive_thread = None

        # C2C state
        self.c2c_rules: List[dict] = []
        self.c2c_listeners: Dict[str, socket.socket] = {}
        self.c2c_uid_to_rule: Dict[bytes, str] = {}

    def set_running(self, running: bool):
        self.running = running
        if running and not self.receive_thread:
            self.start_receive_thread()

    def update_websocket(self, ws):
        """No-op: ws_sender reference is stable; connection is updated inside WsSender."""
        pass

    def start_receive_thread(self):
        self.receive_thread = Thread(target=self._udp_receive_loop, daemon=True)
        self.receive_thread.start()

    def setup_c2c_udp_listeners(self, c2c_rules: List[dict]):
        for rule_name, listener in list(self.c2c_listeners.items()):
            try:
                listener.close()
                LoggerFactory.get_logger().info('Cleaned old C2C UDP listener: %s' % rule_name)
            except Exception as e:
                LoggerFactory.get_logger().error('Failed to close old C2C UDP listener %s: %s' % (rule_name, e))

        self.c2c_listeners.clear()
        self.c2c_uid_to_rule.clear()

        self.c2c_rules = c2c_rules
        LoggerFactory.get_logger().info('Setting up %d C2C UDP listeners' % len(c2c_rules))

        for rule in c2c_rules:
            if rule['protocol'] != 'udp':
                continue

            rule_name = rule['name']
            local_ip = rule['local_ip']
            local_port = rule['local_port']

            try:
                listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((local_ip, local_port))

                self.c2c_listeners[rule_name] = listener
                LoggerFactory.get_logger().info('C2C UDP listener created: %s on %s:%d' % (rule_name, local_ip, local_port))

                receive_thread = threading.Thread(
                    target=self.handle_c2c_udp_data,
                    args=(listener, rule),
                    daemon=True
                )
                receive_thread.start()

            except Exception as e:
                LoggerFactory.get_logger().error('Failed to create C2C UDP listener %s: %s' % (rule_name, e))
                LoggerFactory.get_logger().error(traceback.format_exc())

    def handle_c2c_udp_data(self, listener: socket.socket, rule: dict):
        rule_name = rule['name']
        target_client = rule['target_client']
        protocol = rule['protocol']
        speed_limit = rule.get('speed_limit', 0.0)

        use_direct_mode = 'target_ip' in rule and 'target_port' in rule

        addr_to_uid: Dict[tuple, bytes] = {}

        LoggerFactory.get_logger().info('C2C UDP receive thread started: %s' % rule_name)

        while self.running:
            try:
                data, source_addr = listener.recvfrom(65536)
                if not data:
                    continue

                LoggerFactory.get_logger().debug('C2C UDP data received: %s from %s, len: %d' % (rule_name, source_addr, len(data)))

                if source_addr not in addr_to_uid:
                    uid = os.urandom(4)
                    addr_to_uid[source_addr] = uid

                    with self.lock:
                        self.c2c_uid_to_rule[uid] = rule_name

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
                    LoggerFactory.get_logger().info('C2C UDP forward request sent: %s UID: %s' % (rule_name, uid.hex()))

                    ip_port = '%s:%s' % (source_addr[0], source_addr[1])
                    speed_limiter = SpeedLimiter(speed_limit) if speed_limit > 0 else None
                    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    connection = UdpSocketConnection(uid, udp_socket, rule_name, ip_port, self.ws, speed_limiter)
                    connection.target_address = source_addr

                    with self.lock:
                        self.uid_to_connection[uid] = connection

                else:
                    uid = addr_to_uid[source_addr]

                send_message: MessageEntity = {
                    'type_': MessageTypeConstant.WEBSOCKET_OVER_UDP,
                    'data': {
                        'name': rule_name,
                        'data': data,
                        'uid': uid,
                        'ip_port': '%s:%s' % (source_addr[0], source_addr[1])
                    }
                }
                self.ws.send(
                    NatSerialization.dumps(send_message, ContextUtils.get_password(), self.compress_support, self.protocol_version)
                )
                LoggerFactory.get_logger().debug('C2C UDP data forwarded: %s UID: %s, len: %d' % (rule_name, uid.hex(), len(data)))

            except OSError:
                LoggerFactory.get_logger().info('C2C UDP listener closed: %s' % rule_name)
                break
            except Exception as e:
                LoggerFactory.get_logger().error('C2C UDP data processing error %s: %s' % (rule_name, e))
                LoggerFactory.get_logger().error(traceback.format_exc())

    def _udp_receive_loop(self):
        LoggerFactory.get_logger().info('UDP receive thread started')
        while self.running:
            connections = list(self.uid_to_connection.values())
            for conn in connections:
                try:
                    conn.socket.setblocking(False)
                    try:
                        data, addr = conn.socket.recvfrom(65536)
                        if data:
                            self._handle_udp_data(conn, data, addr)
                    except (BlockingIOError, socket.error):
                        pass
                except Exception as e:
                    LoggerFactory.get_logger().error('UDP data receive error: %s' % e)
                    LoggerFactory.get_logger().error(traceback.format_exc())

            time.sleep(0.001)

    def _handle_udp_data(self, conn: UdpSocketConnection, data: bytes, addr):
        if conn.speed_limiter and data:
            wait_time = conn.speed_limiter.acquire(len(data))
            if wait_time > 0:
                time.sleep(wait_time)

        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_UDP,
            'data': {
                'name': conn.name,
                'data': data,
                'uid': conn.uid,
                'ip_port': conn.ip_port
            }
        }

        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug('Sending UDP to WebSocket, uid: %s, len: %d' % (conn.uid, len(data)))

        try:
            self.ws.send(
                NatSerialization.dumps(send_message, ContextUtils.get_password(), self.compress_support, self.protocol_version)
            )
        except Exception as e:
            LoggerFactory.get_logger().error('Failed to send UDP to WebSocket: %s' % e)

    def create_udp_socket(self, name: str, uid: bytes, ip_port: str, speed_limiter: SpeedLimiter) -> bool:
        if uid in self.uid_to_connection:
            return True

        with self.lock:
            if uid in self.uid_to_connection:
                return True

            try:
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug('Creating UDP socket %s, %s' % (name, uid))

                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                connection = UdpSocketConnection(uid, s, name, ip_port, self.ws, speed_limiter)
                self.uid_to_connection[uid] = connection

                LoggerFactory.get_logger().info('UDP socket created successfully, name: %s, uid: %s' % (name, uid))
                return True
            except Exception as e:
                LoggerFactory.get_logger().error('Failed to create UDP socket: %s' % e)
                LoggerFactory.get_logger().error(traceback.format_exc())
                return False

    def send_by_uid(self, uid: bytes, data: bytes):
        connection = self.uid_to_connection.get(uid)
        if not connection:
            LoggerFactory.get_logger().warning('UDP UID %s not found' % uid)
            return False

        try:
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug('Starting to send UDP data, UID: %s, len: %d' % (uid, len(data)))

            if connection.target_address:
                connection.socket.sendto(data, connection.target_address)
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug('UDP sent successfully, target: %s, len: %d' % (connection.target_address, len(data)))
                return True
            else:
                LoggerFactory.get_logger().warning('UDP connection has no target address, UID: %s' % uid)
                return False
        except Exception as e:
            LoggerFactory.get_logger().error('Failed to send UDP data: %s' % e)
            LoggerFactory.get_logger().error(traceback.format_exc())
            return False

    def close_connection(self, uid: bytes):
        with self.lock:
            if uid in self.uid_to_connection:
                connection = self.uid_to_connection.pop(uid)
                try:
                    connection.socket.close()
                    LoggerFactory.get_logger().info('UDP connection closed, UID: %s' % uid)
                except Exception as e:
                    LoggerFactory.get_logger().error('Failed to close UDP connection: %s' % e)
            else:
                LoggerFactory.get_logger().warning('UID %s UDP connection not found' % uid)

    def close(self):
        self.running = False

        for rule_name, listener in self.c2c_listeners.items():
            try:
                listener.close()
                LoggerFactory.get_logger().info('C2C UDP listener closed: %s' % rule_name)
            except Exception as e:
                LoggerFactory.get_logger().error('Failed to close C2C UDP listener %s: %s' % (rule_name, e))

        self.c2c_listeners.clear()
        self.c2c_rules.clear()
        self.c2c_uid_to_rule.clear()

        with self.lock:
            for uid, conn in list(self.uid_to_connection.items()):
                try:
                    conn.socket.close()
                except Exception:
                    pass
            self.uid_to_connection.clear()
