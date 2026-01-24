import logging
import os
import queue
import socket
import threading
import time
import traceback
from threading import Lock
from typing import Dict, List

from common import websocket
from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.pool import SelectPool
from common.register_append_data import ResisterAppendData
from common.speed_limit import SpeedLimiter
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class MessageSender:
    """
    Dual priority queue message sender
    - High priority queue: for heartbeat and control messages (latency < 10ms)
    - Normal queue: for business data
    """

    def __init__(self, ws):
        # Dual priority queues
        self.high_priority_queue = queue.Queue(maxsize=64)     # High priority: control messages
        self.normal_queue = queue.Queue(maxsize=1024)          # Normal priority: business data
        self.ws = ws
        self.running = False
        self.sender_thread = threading.Thread(target=self.send_messages)

    def send_messages(self):
        """Priority send loop"""
        while self.running or not self.normal_queue.empty() or not self.high_priority_queue.empty():
            try:
                send_bytes = None

                # 1. Process high priority queue first (non-blocking)
                try:
                    send_bytes = self.high_priority_queue.get_nowait()
                except queue.Empty:
                    pass

                # 2. If high priority queue is empty, get from normal queue (blocking)
                if send_bytes is None:
                    try:
                        send_bytes = self.normal_queue.get(timeout=1)
                    except queue.Empty:
                        continue

                # 3. Send message
                self.ws.send(send_bytes, opcode=websocket.ABNF.OPCODE_BINARY)

                # Mark task as done
                try:
                    self.high_priority_queue.task_done()
                except ValueError:
                    self.normal_queue.task_done()

            except Exception as e:
                LoggerFactory.get_logger().error(f"Failed to send message: {e}")

    def enqueue_message(self, message, high_priority=False):
        """
        Add message to queue
        :param message: Message content
        :param high_priority: Whether high priority (heartbeat, control messages)
        """
        if self.running:
            if high_priority:
                try:
                    self.high_priority_queue.put_nowait(message)
                except queue.Full:
                    LoggerFactory.get_logger().warning("High priority queue full")
            else:
                try:
                    self.normal_queue.put(message, timeout=5)
                except queue.Full:
                    LoggerFactory.get_logger().warning("Normal queue full")
        else:
            LoggerFactory.get_logger().warning("WebSocket is not running. Cannot enqueue message.")

    def start(self):
        self.running = True
        self.sender_thread.start()

    def stop(self):
        """Safe stop: send high priority first, then normal queue"""
        def safe_stop():
            # 1. Send high priority queue first
            while not self.high_priority_queue.empty():
                try:
                    message = self.high_priority_queue.get_nowait()
                    self.ws.send(message, opcode=websocket.ABNF.OPCODE_BINARY)
                    self.high_priority_queue.task_done()
                except queue.Empty:
                    break
                except Exception as e:
                    LoggerFactory.get_logger().error(f"Failed to send high priority message during stop: {e}")

            # 2. Then send normal queue
            while not self.normal_queue.empty():
                try:
                    message = self.normal_queue.get_nowait()
                    self.ws.send(message, opcode=websocket.ABNF.OPCODE_BINARY)
                    self.normal_queue.task_done()
                except queue.Empty:
                    break
                except Exception as e:
                    LoggerFactory.get_logger().error(f"Failed to send normal message during stop: {e}")

            self.running = False
        safe_stop()


class PrivateSocketConnection:
    """Client connecting to internal network port"""

    def __init__(self, uid: bytes, s: socket.socket, name: str, ws):
        self.uid: bytes = uid
        self.socket: socket.socket = s
        self.name: str = name
        self.sender = MessageSender(ws)
        self.sender.start()


class TcpForwardClient:
    def __init__(self, ws: websocket, compress_support: bool, protocol_version: int):
        self.uid_to_socket_connection: Dict[bytes, PrivateSocketConnection] = dict()
        self.socket_to_socket_connection: Dict[socket.socket, PrivateSocketConnection] = dict()
        self.compress_support: bool = compress_support
        self.protocol_version: int = protocol_version
        self.ws = ws
        self.lock = Lock()
        self.socket_event_loop = SelectPool()

        # C2C client-to-client forward state
        self.c2c_rules: List[dict] = []                     # C2C rules list
        self.c2c_listeners: Dict[str, socket.socket] = {}   # rule_name → listener socket
        self.c2c_uid_to_rule: Dict[bytes, str] = {}         # UID → rule_name

        # N4 Tunnel Manager for P2P data transfer
        self.tunnel_manager = None

        # Pending data buffer: uid -> list of (data, timestamp)
        # Used to buffer P2P data that arrives before connection is established
        self.pending_data: Dict[bytes, list] = {}
        self.pending_data_lock = Lock()
        self.pending_data_timeout = 10  # seconds to keep pending data

    def set_running(self, running: bool):
        self.socket_event_loop.is_running = running

    def update_websocket(self, ws):
        """Update websocket connection reference (for client reconnection)"""
        self.ws = ws
        LoggerFactory.get_logger().info('TCP forward client websocket reference updated')

    def start_forward(self):
        self.socket_event_loop.run()

    def setup_c2c_tcp_listeners(self, c2c_rules: List[dict]):
        """
        Setup C2C TCP listeners
        Create local listeners for each C2C rule to accept connections from local applications
        """
        # Clean old listeners first to avoid port conflicts
        for rule_name, listener in list(self.c2c_listeners.items()):
            try:
                listener.close()
                LoggerFactory.get_logger().info(f'Cleaned old C2C TCP listener: {rule_name}')
            except Exception as e:
                LoggerFactory.get_logger().error(f'Failed to close old C2C TCP listener {rule_name}: {e}')

        self.c2c_listeners.clear()
        self.c2c_uid_to_rule.clear()

        self.c2c_rules = c2c_rules
        LoggerFactory.get_logger().info(f'Setting up {len(c2c_rules)} C2C TCP listeners')

        for rule in c2c_rules:
            if rule['protocol'] != 'tcp':
                continue

            rule_name = rule['name']
            local_ip = rule['local_ip']
            local_port = rule['local_port']

            try:
                # Create listener socket
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((local_ip, local_port))
                listener.listen(128)

                self.c2c_listeners[rule_name] = listener
                LoggerFactory.get_logger().info(f'C2C TCP listener created: {rule_name} on {local_ip}:{local_port}')

                # Start separate thread to handle connection acceptance
                accept_thread = threading.Thread(
                    target=self.handle_c2c_tcp_accept,
                    args=(listener, rule),
                    daemon=True
                )
                accept_thread.start()

            except Exception as e:
                LoggerFactory.get_logger().error(f'Failed to create C2C TCP listener {rule_name}: {e}')
                LoggerFactory.get_logger().error(traceback.format_exc())

    def handle_c2c_tcp_accept(self, listener: socket.socket, rule: dict):
        """
        Handle C2C TCP connection acceptance
        Loop to accept connections from local applications, send CLIENT_TO_CLIENT_FORWARD request for each connection
        """
        rule_name = rule['name']
        target_client = rule['target_client']
        protocol = rule['protocol']
        speed_limit = rule.get('speed_limit', 0.0)

        # Check if using direct mode (target_ip + target_port) or service mode (target_service)
        use_direct_mode = 'target_ip' in rule and 'target_port' in rule

        LoggerFactory.get_logger().info(f'C2C TCP accept thread started: {rule_name}')

        while True:
            try:
                client_socket, client_addr = listener.accept()
                LoggerFactory.get_logger().info(f'C2C TCP connection accepted: {rule_name} from {client_addr}')

                # Enable TCP_NODELAY to reduce latency
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                # Generate unique UID
                uid = os.urandom(4)

                # Store UID → rule_name mapping
                with self.lock:
                    self.c2c_uid_to_rule[uid] = rule_name

                    # Create connection object
                    connection = PrivateSocketConnection(uid, client_socket, rule_name, self.ws)
                    self.uid_to_socket_connection[uid] = connection
                    self.socket_to_socket_connection[client_socket] = connection

                # Register UID to tunnel_manager for P2P routing
                if self.tunnel_manager:
                    self.tunnel_manager.register_uid(uid, target_client)
                    LoggerFactory.get_logger().info(f'Registered UID {uid.hex()} to peer {target_client}')

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
                LoggerFactory.get_logger().info(f'C2C forward request sent: {rule_name} UID: {uid.hex()}')

                # Register to event loop, start forwarding data
                speed_limiter = SpeedLimiter(speed_limit) if speed_limit > 0 else None
                self.socket_event_loop.register(client_socket, ResisterAppendData(self.handle_message, speed_limiter))

            except OSError as e:
                # Listener closed, exit loop
                LoggerFactory.get_logger().info(f'C2C TCP listener closed: {rule_name}')
                break
            except Exception as e:
                LoggerFactory.get_logger().error(f'C2C TCP accept connection error {rule_name}: {e}')
                LoggerFactory.get_logger().error(traceback.format_exc())


    def handle_message(self, each: socket.socket, data: ResisterAppendData):
        connection = self.socket_to_socket_connection.get(each)
        if not connection:
            return

        try:
            recv = each.recv(data.read_size)
        except OSError:
            recv = b''

        # --- 发送端限速：在发送前等待 ---
        if data.speed_limiter and recv:
            wait_time = data.speed_limiter.acquire(len(recv))
            if wait_time > 0:
                time.sleep(wait_time)

        # --- 无缝切换逻辑 ---
        if self.tunnel_manager:
            # 尝试走 P2P 隧道
            if self.tunnel_manager.send_data(connection.uid, recv):
                # 如果发送成功返回 True，逻辑结束
                if not recv: # 本地连接关闭，通知隧道
                    self.close_connection(each)
                return

                # --- 回退/初始逻辑：走 WebSocket ---
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_TCP,
            'data': {
                'name': connection.name,
                'data': recv,
                'uid': connection.uid,
                'ip_port': ''
            }
        }

        connection.sender.enqueue_message(
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
            if uid in self.uid_to_socket_connection:  # Check again
                return True
            try:
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'create socket {name}, {uid}')

                # Validate ip_port before splitting
                if not ip_port or ':' not in ip_port:
                    LoggerFactory.get_logger().error(f'Invalid ip_port: {repr(ip_port)}, name: {name}, uid: {uid.hex()}')
                    return False

                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5)
                # Enable TCP_NODELAY to reduce latency (disable Nagle algorithm)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                connection = PrivateSocketConnection(uid, s, name, self.ws)
                self.socket_to_socket_connection[s] = connection
                ip, port = ip_port.split(':')
                try:
                    s.connect((ip, int(port)))

                    # Optimistic send mode: send confirmation message immediately after connection success
                    confirm_message: MessageEntity = {
                        'type_': MessageTypeConstant.CONNECT_CONFIRMED,
                        'data': {
                            'name': name,
                            'data': b'',
                            'uid': uid,
                            'ip_port': ip_port
                        }
                    }
                    self.ws.send(NatSerialization.dumps(confirm_message, ContextUtils.get_password(), self.compress_support, self.protocol_version), websocket.ABNF.OPCODE_BINARY)
                    if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                        LoggerFactory.get_logger().debug(f'Connection confirmation message sent uid: {uid}')

                except OSError as e:
                    LoggerFactory.get_logger().info(f'connection error, {e}')

                    # Optimistic send mode: send failure message after connection failure
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
                        self.ws.send(NatSerialization.dumps(fail_message, ContextUtils.get_password(), self.compress_support, self.protocol_version), websocket.ABNF.OPCODE_BINARY)
                    except Exception as send_err:
                        LoggerFactory.get_logger().error(f'Failed to send connection failure message: {send_err}')

                    self.close_connection(s)
                    self.close_remote_socket(connection)
                    return False

                self.uid_to_socket_connection[uid] = connection
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'register socket {name}, {uid}')
                self.socket_event_loop.register(s, ResisterAppendData(self.handle_message, speed_limiter))
                if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                    LoggerFactory.get_logger().debug(f'register socket success {name}, {uid}')

                # Process any pending data that arrived before connection was established
                self._flush_pending_data(uid, s)

                return True
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
                if connection:
                    self.close_remote_socket(connection)
                return False

    def close_connection(self, socket_client: socket.socket):
        LoggerFactory.get_logger().info(f'Closing socket {socket_client}')
        if socket_client in self.socket_to_socket_connection:
            connection: PrivateSocketConnection = self.socket_to_socket_connection.pop(socket_client)
            self.socket_event_loop.unregister(socket_client)
            try:
                socket_client.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                LoggerFactory.get_logger().warn(f'Shutdown OS error {e}')
            socket_client.close()
            connection.sender.stop()
            LoggerFactory.get_logger().info(f'Socket closed successfully {socket_client}')
            if connection.uid in self.uid_to_socket_connection:
                self.uid_to_socket_connection.pop(connection.uid)
            self.close_remote_socket(connection)

    def close(self):
        LoggerFactory.get_logger().info(f'Starting to close {self.c2c_listeners}')
        with self.lock:
            # Close all C2C listeners
            for rule_name, listener in self.c2c_listeners.items():
                try:
                    try:
                        listener.shutdown(socket.SHUT_RDWR)
                    except OSError as e:
                        LoggerFactory.get_logger().warn(f'Shutdown OS error {e}')
                    listener.close()
                    LoggerFactory.get_logger().info(f'C2C TCP listener closed: {rule_name}')
                except Exception as e:
                    LoggerFactory.get_logger().error(f'Failed to close C2C TCP listener {rule_name}: {e}')

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
                        LoggerFactory.get_logger().warn(f'Shutdown OS error {e}')
                    s.close()
                except Exception:
                    LoggerFactory.get_logger().error(traceback.format_exc())
                c.sender.stop()
            self.uid_to_socket_connection.clear()
            self.socket_to_socket_connection.clear()
            self.set_running(False)
            self.socket_event_loop.clear()

    def close_remote_socket(self, connection: PrivateSocketConnection):
        # if name is None:
        #     connection = self.uid_to_socket_connection.get(uid)
        #     if not connection:
        #         return
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
        self.ws.send(NatSerialization.dumps(send_message, ContextUtils.get_password(), self.compress_support, self.protocol_version), websocket.ABNF.OPCODE_BINARY)
        LoggerFactory.get_logger().debug(f'Send to websocket cost time {time.time() - start_time}')

    def send_by_uid(self, uid: bytes, msg: bytes):
        connection = self.uid_to_socket_connection.get(uid)
        if not connection:
            # Connection not established yet, buffer the data
            # This handles the race condition where P2P data arrives before
            # the REQUEST_TO_CONNECT message establishes the connection
            if msg:  # Only buffer non-empty data
                with self.pending_data_lock:
                    if uid not in self.pending_data:
                        self.pending_data[uid] = []
                    self.pending_data[uid].append((msg, time.time()))
                    LoggerFactory.get_logger().debug(
                        f'Buffered {len(msg)} bytes for UID {uid.hex()} (connection pending)'
                    )
            return
        try:
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'Starting to send to socket uid: {uid}, length: {len(msg)}')
            s = connection.socket
            s.sendall(msg)
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f'Finished sending to socket uid {uid}, length: {len(msg)}')
            if not msg:
                self.close_connection(s)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
            # After error, send empty message, server will close connection
            self.close_remote_socket(connection)

    def _flush_pending_data(self, uid: bytes, sock: socket.socket):
        """
        Flush any pending data that was buffered before connection was established.
        This solves the race condition where P2P data arrives before REQUEST_TO_CONNECT.
        """
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
            # Skip expired data
            if now - timestamp > self.pending_data_timeout:
                expired_count += 1
                continue

            try:
                sock.sendall(data)
                total_sent += len(data)
            except Exception as e:
                LoggerFactory.get_logger().error(f'Failed to flush pending data for UID {uid.hex()}: {e}')
                break

        if total_sent > 0 or expired_count > 0:
            LoggerFactory.get_logger().info(
                f'Flushed pending data for UID {uid.hex()}: '
                f'{total_sent} bytes sent, {expired_count} expired packets dropped'
            )
