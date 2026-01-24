import asyncio
import logging
import socket
import time
import traceback
import os
from typing import Dict, Set, List, Tuple
from functools import partial
import threading
from asyncio import Lock as AsyncioLock

import tornado

from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.speed_limit import SpeedLimiter
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class UdpEndpoint:
    """UDP endpoint that maintains the mapping between client and server addresses"""
    def __init__(self, uid: bytes, address: Tuple[str, int], server):
        self.uid = uid
        self.address = address
        self.server = server
        self.last_active_time = time.time()


class UdpPublicSocketServer:
    """UDP server listening on a public network port"""
    def __init__(self, s: socket.socket, name: str, ip_port: str, websocket_handler, speed_limit_size: float):
        self.socket_server = s
        self.name = name
        self.ip_port = ip_port
        self.websocket_handler = websocket_handler
        self.speed_limit_size = speed_limit_size
        self.speed_limiter = SpeedLimiter(speed_limit_size) if speed_limit_size else None
        # UDP must remember each client’s address
        self.addr_to_uid: Dict[str, bytes] = {}
        self.uid_to_endpoint: Dict[bytes, UdpEndpoint] = {}
        self.lock = threading.Lock()

    def add_endpoint(self, address: Tuple[str, int]) -> bytes:
        """Add a new UDP endpoint and return the generated UID"""
        addr_key = f"{address[0]}:{address[1]}"
        with self.lock:
            if addr_key in self.addr_to_uid:
                uid = self.addr_to_uid[addr_key]
                self.uid_to_endpoint[uid].last_active_time = time.time()
                return uid

            uid = os.urandom(4)
            endpoint = UdpEndpoint(uid, address, self)
            self.uid_to_endpoint[uid] = endpoint
            self.addr_to_uid[addr_key] = uid
            return uid

    def get_endpoint_by_uid(self, uid: bytes) -> UdpEndpoint:
        """Get UDP endpoint by UID"""
        with self.lock:
            return self.uid_to_endpoint.get(uid)

    def remove_endpoint(self, uid: bytes):
        """Remove a UDP endpoint"""
        with self.lock:
            if uid in self.uid_to_endpoint:
                endpoint = self.uid_to_endpoint[uid]
                addr_key = f"{endpoint.address[0]}:{endpoint.address[1]}"
                if addr_key in self.addr_to_uid:
                    del self.addr_to_uid[addr_key]
                del self.uid_to_endpoint[uid]

    def clean_inactive_endpoints(self, max_inactive_time: int = 300):
        """Clean up inactive endpoints"""
        current_time = time.time()
        with self.lock:
            inactive_uids = []
            for uid, endpoint in self.uid_to_endpoint.items():
                if current_time - endpoint.last_active_time > max_inactive_time:
                    inactive_uids.append(uid)

            for uid in inactive_uids:
                self.remove_endpoint(uid)


class UdpForwardClient:
    _instance = None

    def __init__(self, loop=None, tornado_loop=None):
        self.loop = loop or asyncio.get_event_loop()
        self.tornado_loop = tornado_loop or tornado.ioloop.IOLoop.current()
        self.udp_servers: Dict[int, UdpPublicSocketServer] = {}  # Mapping: port number → server instance
        self.client_name_to_udp_server_set: Dict[str, Set[UdpPublicSocketServer]] = {}
        self.client_name_to_lock: Dict[str, AsyncioLock] = {}
        self.running = True

        # Threads
        self.receive_thread = None
        self.signal_thread = None

        # P2P Signal Port
        self.p2p_signal_port = 19999
        self.p2p_signal_socket = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls(asyncio.get_event_loop(), tornado.ioloop.IOLoop.current())
            cls._instance.start_receive_thread()
        return cls._instance

    def start_receive_thread(self):
        """Start threads"""
        # 1. Business UDP Loop
        if self.receive_thread is None:
            self.receive_thread = threading.Thread(target=self._udp_receive_loop)
            self.receive_thread.daemon = True
            self.receive_thread.start()

        # 2. P2P Signal Loop (DISABLED - now handled by P2PExchangeService)
        # if self.signal_thread is None:
        #     self.signal_thread = threading.Thread(target=self._p2p_signal_loop)
        #     self.signal_thread.daemon = True
        #     self.signal_thread.start()

    def _p2p_signal_loop(self):
        """Loop to handle P2P address refresh signals"""
        try:
            self.p2p_signal_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.p2p_signal_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Listen on 0.0.0.0:19999
            self.p2p_signal_socket.bind(('0.0.0.0', self.p2p_signal_port))
            LoggerFactory.get_logger().info(f"P2P Signal Server started on UDP port {self.p2p_signal_port}")

            while self.running:
                try:
                    data, addr = self.p2p_signal_socket.recvfrom(1024)

                    # Protocol: b'P2P_PING:client_name'
                    if data.startswith(b'P2P_PING:'):
                        try:
                            # Decode client name
                            client_name = data.split(b':')[1].decode('utf-8')

                            # Import here to avoid circular dependency
                            from server.websocket_handler import MyWebSocketaHandler

                            # Find handler by name
                            handler = MyWebSocketaHandler.client_name_to_handler.get(client_name)

                            if handler:
                                # Update public address
                                # Only log if changed to reduce noise
                                if handler.public_port != addr[1] or handler.public_ip != addr[0]:
                                    LoggerFactory.get_logger().info(f"P2P Addr Update [{client_name}]: {addr[0]}:{addr[1]}")

                                handler.public_ip = addr[0]
                                handler.public_port = addr[1]

                        except Exception as e:
                            pass
                            # LoggerFactory.get_logger().warn(f"P2P signal error: {e}")

                except OSError:
                    pass
                except Exception as e:
                    LoggerFactory.get_logger().error(f"P2P signal loop error: {e}")
                    time.sleep(1)

        except Exception as e:
            LoggerFactory.get_logger().error(f"Failed to start P2P Signal Server: {e}")

    def _udp_receive_loop(self):
        """UDP data receiving loop"""
        LoggerFactory.get_logger().info("UDP receive thread start")
        while self.running:
            # Iterate over all UDP servers
            for port, server in list(self.udp_servers.items()):
                try:
                    # Use non-blocking mode to avoid blocking other servers
                    server.socket_server.setblocking(False)
                    try:
                        data, address = server.socket_server.recvfrom(65536)
                        if data:
                            # Process received UDP data
                            self._handle_udp_data(server, data, address)
                    except (BlockingIOError, socket.error):
                        # No data available, continue to next server
                        pass
                except Exception as e:
                    LoggerFactory.get_logger().error(f"UDP receive error: {e}")
                    LoggerFactory.get_logger().error(traceback.format_exc())

            # Prevent high CPU usage
            time.sleep(0.001)

    def _handle_udp_data(self, server: UdpPublicSocketServer, data: bytes, address: Tuple[str, int]):
        """Handle received UDP data"""
        # Add or get the corresponding endpoint
        # LoggerFactory.get_logger().info(f"UDP received {server.socket_server}, len: {len(data)} bytes")

        uid = server.add_endpoint(address)

        # --- 发送端限速：在发送前等待 ---
        if server.speed_limiter and data:
            wait_time = server.speed_limiter.acquire(len(data))
            if wait_time > 0:
                time.sleep(wait_time)

        # Construct a UDP message and forward it to the internal client via WebSocket
        send_message: MessageEntity = {
            'type_': MessageTypeConstant.WEBSOCKET_OVER_UDP,
            'data': {
                'name': server.name,
                'data': data,
                'uid': uid,
                'ip_port': server.ip_port
            }
        }

        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug(f"Send UDP data to WebSocket, uid: {uid}, len: {len(data)}")

        # Schedule WebSocket send operation in the async loop
        handler = server.websocket_handler
        is_compress = handler.compress_support
        protocol_version = handler.protocol_version
        self.tornado_loop.add_callback(
            partial(handler.write_message, NatSerialization.dumps(send_message, ContextUtils.get_password(), is_compress, protocol_version)), True)

    async def register_udp_server(self, port: int, name: str, ip_port: str, websocket_handler, speed_limit_size: float):
        """Register a UDP server"""
        client_name = websocket_handler.client_name
        if client_name not in self.client_name_to_lock:
            self.client_name_to_lock[client_name] = AsyncioLock()

        async with self.client_name_to_lock.get(client_name):
            if port in self.udp_servers:
                LoggerFactory.get_logger().warning(f"UDP port {port} is forbidden")
                return False

            try:
                # Create UDP socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('', port))

                # Create and register UDP server
                server = UdpPublicSocketServer(sock, name, ip_port, websocket_handler, speed_limit_size)
                self.udp_servers[port] = server

                if client_name not in self.client_name_to_udp_server_set:
                    self.client_name_to_udp_server_set[client_name] = set()

                self.client_name_to_udp_server_set[client_name].add(server)
                LoggerFactory.get_logger().info(f"UDP server registered, port: {port}, name: {name}")
                return True
            except Exception as e:
                LoggerFactory.get_logger().error(f"UDP registration failed: {e}")
                LoggerFactory.get_logger().error(traceback.format_exc())
                return False

    async def send_udp(self, uid: bytes, data: bytes, port: int):
        """Send UDP data to a client"""
        if port not in self.udp_servers:
            LoggerFactory.get_logger().warning(f"No UDP server found for port {port}")
            return False

        server = self.udp_servers[port]
        endpoint = server.get_endpoint_by_uid(uid)

        if not endpoint:
            LoggerFactory.get_logger().warning(f"No UDP endpoint found for UID {uid}")
            return False

        try:
            # Send UDP data
            bytes_sent = server.socket_server.sendto(data, endpoint.address)
            if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                LoggerFactory.get_logger().debug(f"UDP data sent successfully, target: {endpoint.address}, length: {bytes_sent}")
            return True
        except Exception as e:
            LoggerFactory.get_logger().error(f"UDP data send failed: {e}")
            return False

    async def close_by_client_name(self, client_name: str):
        """Close all UDP servers associated with a specific client"""
        if client_name not in self.client_name_to_udp_server_set:
            return

        if client_name not in self.client_name_to_lock:
            self.client_name_to_lock[client_name] = AsyncioLock()

        async with self.client_name_to_lock.get(client_name):
            for server in list(self.client_name_to_udp_server_set[client_name]):
                try:
                    # Find and remove the corresponding port
                    for port, srv in list(self.udp_servers.items()):
                        if srv == server:
                            del self.udp_servers[port]
                            break

                    # Close socket
                    server.socket_server.close()
                except Exception as e:
                    LoggerFactory.get_logger().error(f"Failed to close UDP server: {e}")

            # Clean up server set
            self.client_name_to_udp_server_set.pop(client_name, None)

        # Clean up lock
        self.client_name_to_lock.pop(client_name, None)

    def stop(self):
        """Stop the UDP forward client"""
        self.running = False
        for port, server in list(self.udp_servers.items()):
            try:
                server.socket_server.close()
            except Exception:
                pass

        if self.p2p_signal_socket:
            try:
                self.p2p_signal_socket.close()
            except:
                pass

        self.udp_servers.clear()
        self.client_name_to_udp_server_set.clear()