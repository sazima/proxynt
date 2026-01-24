import asyncio
import logging
import os
import socket
import time
import traceback
import threading
from typing import Dict, List, Set
from threading import Thread

from common import websocket
from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.speed_limit import SpeedLimiter
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class UdpSocketConnection:
    """Client connecting to internal UDP port"""
    def __init__(self, uid: bytes, socket_obj: socket.socket, name: str, ip_port: str, ws, speed_limiter=None):
        self.uid = uid
        self.socket = socket_obj
        self.name = name
        self.ip_port = ip_port
        self.ws = ws
        self.speed_limiter = speed_limiter
        self.target_address = None
        self.last_active_time = time.time()

        # Parse target IP and port
        if ":" in ip_port:
            ip, port_str = ip_port.split(":")
            self.target_address = (ip, int(port_str))
        else:
            self.target_address = None

class UdpForwardClient:
    """UDP forward client"""
    def __init__(self, ws, compress_support: bool, protocol_version: int):
        self.uid_to_connection: Dict[bytes, UdpSocketConnection] = {}
        self.ws = ws
        self.compress_support = compress_support
        self.protocol_version = protocol_version
        self.running = True
        self.lock = threading.Lock()

        # Start UDP receive thread
        self.receive_thread = None

        # C2C client-to-client forward state
        self.c2c_rules: List[dict] = []                     # C2C rules list
        self.c2c_listeners: Dict[str, socket.socket] = {}   # rule_name → listener socket
        self.c2c_uid_to_rule: Dict[bytes, str] = {}         # UID → rule_name

    def set_running(self, running: bool):
        """Set running state"""
        self.running = running
        if running and not self.receive_thread:
            self.start_receive_thread()

    def update_websocket(self, ws):
        """Update websocket connection reference (for client reconnection)"""
        self.ws = ws
        LoggerFactory.get_logger().info('UDP forward client websocket reference updated')

    def start_receive_thread(self):
        """Start UDP receive thread"""
        self.receive_thread = Thread(target=self._udp_receive_loop)
        self.receive_thread.daemon = True
        self.receive_thread.start()

    def setup_c2c_udp_listeners(self, c2c_rules: List[dict]):
        """
        Setup C2C UDP listeners
        Create local listeners for each C2C UDP rule to receive UDP data from local applications
        """
        # Clean old listeners first to avoid port conflicts
        for rule_name, listener in list(self.c2c_listeners.items()):
            try:
                listener.close()
                LoggerFactory.get_logger().info(f'Cleaned old C2C UDP listener: {rule_name}')
            except Exception as e:
                LoggerFactory.get_logger().error(f'Failed to close old C2C UDP listener {rule_name}: {e}')

        self.c2c_listeners.clear()
        self.c2c_uid_to_rule.clear()

        self.c2c_rules = c2c_rules
        LoggerFactory.get_logger().info(f'Setting up {len(c2c_rules)} C2C UDP listeners')

        for rule in c2c_rules:
            if rule['protocol'] != 'udp':
                continue

            rule_name = rule['name']
            local_ip = rule['local_ip']
            local_port = rule['local_port']
            target_client = rule['target_client']
            target_service = rule['target_service']
            speed_limit = rule.get('speed_limit', 0.0)

            try:
                # Create UDP listener socket
                listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((local_ip, local_port))

                self.c2c_listeners[rule_name] = listener
                LoggerFactory.get_logger().info(f'C2C UDP listener created: {rule_name} on {local_ip}:{local_port}')

                # Start separate thread to handle UDP data reception
                receive_thread = threading.Thread(
                    target=self.handle_c2c_udp_data,
                    args=(listener, rule),
                    daemon=True
                )
                receive_thread.start()

            except Exception as e:
                LoggerFactory.get_logger().error(f'Failed to create C2C UDP listener {rule_name}: {e}')
                LoggerFactory.get_logger().error(traceback.format_exc())

    def handle_c2c_udp_data(self, listener: socket.socket, rule: dict):
        """
        Handle C2C UDP data reception
        Loop to receive UDP data from local applications and forward to target client
        """
        rule_name = rule['name']
        target_client = rule['target_client']
        protocol = rule['protocol']
        speed_limit = rule.get('speed_limit', 0.0)

        # Check if using direct mode (target_ip + target_port) or service mode (target_service)
        use_direct_mode = 'target_ip' in rule and 'target_port' in rule

        # C2C UDP connection: source address → UID mapping (each source port corresponds to a UID)
        addr_to_uid: Dict[tuple, bytes] = {}

        LoggerFactory.get_logger().info(f'C2C UDP receive thread started: {rule_name}')

        while self.running:
            try:
                # Receive UDP data
                data, source_addr = listener.recvfrom(65536)
                if not data:
                    continue

                LoggerFactory.get_logger().debug(f'C2C UDP data received: {rule_name} from {source_addr}, len: {len(data)}')

                # Find or create UID
                if source_addr not in addr_to_uid:
                    # First data from this source address, generate UID and send forward request
                    uid = os.urandom(4)
                    addr_to_uid[source_addr] = uid

                    with self.lock:
                        self.c2c_uid_to_rule[uid] = rule_name

                    # Send CLIENT_TO_CLIENT_FORWARD message to server
                    forward_data = {
                        'uid': uid,
                        'target_client': target_client,
                        'source_rule_name': rule_name,
                        'protocol': protocol
                    }

                    # Add target_ip and target_port for direct mode, or target_service for service mode
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
                        NatSerialization.dumps(forward_message, ContextUtils.get_password(), self.compress_support, self.protocol_version),
                        websocket.ABNF.OPCODE_BINARY
                    )
                    LoggerFactory.get_logger().info(f'C2C UDP forward request sent: {rule_name} UID: {uid.hex()}')

                    # Create UDP connection object to receive reply data
                    ip_port = f"{source_addr[0]}:{source_addr[1]}"
                    speed_limiter = SpeedLimiter(speed_limit) if speed_limit > 0 else None
                    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    connection = UdpSocketConnection(uid, udp_socket, rule_name, ip_port, self.ws, speed_limiter)
                    connection.target_address = source_addr  # Send reply data to source address

                    with self.lock:
                        self.uid_to_connection[uid] = connection

                else:
                    uid = addr_to_uid[source_addr]

                # Forward UDP data to server
                send_message: MessageEntity = {
                    'type_': MessageTypeConstant.WEBSOCKET_OVER_UDP,
                    'data': {
                        'name': rule_name,
                        'data': data,
                        'uid': uid,
                        'ip_port': f"{source_addr[0]}:{source_addr[1]}"
                    }
                }
                self.ws.send(
                    NatSerialization.dumps(send_message, ContextUtils.get_password(), self.compress_support, self.protocol_version),
                    websocket.ABNF.OPCODE_BINARY
                )
                LoggerFactory.get_logger().debug(f'C2C UDP data forwarded: {rule_name} UID: {uid.hex()}, len: {len(data)}')

            except OSError as e:
                # Listener closed, exit loop
                LoggerFactory.get_logger().info(f'C2C UDP listener closed: {rule_name}')
                break
            except Exception as e:
                LoggerFactory.get_logger().error(f'C2C UDP data processing error {rule_name}: {e}')
                LoggerFactory.get_logger().error(traceback.format_exc())

    def _udp_receive_loop(self):
        """UDP data receive loop"""
        LoggerFactory.get_logger().info("UDP receive thread started")
        while self.running:
            # Copy connection list to avoid modification during iteration
            connections = list(self.uid_to_connection.values())
            for conn in connections:
                try:
                    # Non-blocking receive
                    conn.socket.setblocking(False)
                    try:
                        data, addr = conn.socket.recvfrom(65536)
                        if data:
                            # Process received UDP data
                            self._handle_udp_data(conn, data, addr)
                    except (BlockingIOError, socket.error):
                        # No data available, continue to next connection
                        pass
                except Exception as e:
                    LoggerFactory.get_logger().error(f"UDP data receive error: {e}")
                    LoggerFactory.get_logger().error(traceback.format_exc())

            # Prevent high CPU usage
            time.sleep(0.001)

    def _handle_udp_data(self, conn: UdpSocketConnection, data: bytes, addr):
        """Handle received UDP data"""
        # --- 发送端限速：在发送前等待 ---
        if conn.speed_limiter and data:
            wait_time = conn.speed_limiter.acquire(len(data))
            if wait_time > 0:
                time.sleep(wait_time)

        # Construct UDP message and send to public server via WebSocket
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
            LoggerFactory.get_logger().debug(f'Sending UDP to WebSocket, uid: {conn.uid}, len: {len(data)}')

        try:
            self.ws.send(NatSerialization.dumps(send_message, ContextUtils.get_password(), self.compress_support, self.protocol_version), websocket.ABNF.OPCODE_BINARY)
        except Exception as e:
            LoggerFactory.get_logger().error(f"Failed to send UDP to WebSocket: {e}")

    def create_udp_socket(self, name: str, uid: bytes, ip_port: str, speed_limiter: SpeedLimiter) -> bool:
        """Create UDP socket"""
        if uid in self.uid_to_connection:
            return True

        with self.lock:
            if uid in self.uid_to_connection:  # Check again if already exists
                return True

            try:
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'Creating UDP socket {name}, {uid}')

                # Create UDP socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

                # Create connection object
                connection = UdpSocketConnection(uid, s, name, ip_port, self.ws, speed_limiter)
                self.uid_to_connection[uid] = connection

                LoggerFactory.get_logger().info(f'UDP socket created successfully, name: {name}, uid: {uid}')
                return True
            except Exception as e:
                LoggerFactory.get_logger().error(f"Failed to create UDP socket: {e}")
                LoggerFactory.get_logger().error(traceback.format_exc())
                return False

    def send_by_uid(self, uid: bytes, data: bytes):
        """Send UDP data by UID"""
        connection = self.uid_to_connection.get(uid)
        if not connection:
            LoggerFactory.get_logger().warning(f"UDP UID {uid} not found")
            return False

        try:
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'Starting to send UDP data, UID: {uid}, len: {len(data)}')

            if connection.target_address:
                connection.socket.sendto(data, connection.target_address)
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'UDP sent successfully, target: {connection.target_address}, len: {len(data)}')
                return True
            else:
                LoggerFactory.get_logger().warning(f"UDP connection has no target address, UID: {uid}")
                return False
        except Exception as e:
            LoggerFactory.get_logger().error(f"Failed to send UDP data: {e}")
            LoggerFactory.get_logger().error(traceback.format_exc())
            return False

    def close_connection(self, uid: bytes):
        """Close UDP connection"""
        with self.lock:
            if uid in self.uid_to_connection:
                connection = self.uid_to_connection.pop(uid)
                try:
                    connection.socket.close()
                    LoggerFactory.get_logger().info(f'UDP connection closed, UID: {uid}')
                except Exception as e:
                    LoggerFactory.get_logger().error(f"Failed to close UDP connection: {e}")
            else:
                LoggerFactory.get_logger().warning(f"UID {uid} UDP connection not found")

    def close(self):
        """Close all UDP connections"""
        self.running = False

        # Close all C2C UDP listeners
        for rule_name, listener in self.c2c_listeners.items():
            try:
                listener.close()
                LoggerFactory.get_logger().info(f'C2C UDP listener closed: {rule_name}')
            except Exception as e:
                LoggerFactory.get_logger().error(f'Failed to close C2C UDP listener {rule_name}: {e}')

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