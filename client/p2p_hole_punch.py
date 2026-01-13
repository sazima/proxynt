"""
UDP Hole Punching Module for P2P Connection (Enhanced with N4 Strategy)
"""
import os
import socket
import select
import threading
import time
import traceback
from typing import Dict, Optional, Callable, List

from common.logger_factory import LoggerFactory
from constant.system_constant import SystemConstant


class P2PConnection:
    """Represents a P2P connection attempt"""
    def __init__(self, uid: bytes, remote_ip: str, remote_base_port: int):
        self.uid = uid
        self.remote_ip = remote_ip
        self.remote_base_port = remote_base_port

        # N4 Strategy: Use multiple sockets
        self.sockets: List[socket.socket] = []

        # The successfully connected socket and address
        self.active_socket: Optional[socket.socket] = None
        self.active_remote_addr: Optional[tuple] = None

        self.established = False
        self.last_receive_time = 0
        self.last_send_time = 0


class P2PHolePunch:
    """UDP Hole Punching Manager with Port Prediction"""

    def __init__(self):
        self.connections: Dict[bytes, P2PConnection] = {}
        self.lock = threading.Lock()
        self.running = False
        self.receive_thread: Optional[threading.Thread] = None

        # N4 Configuration
        self.socket_count = 25       # Number of local sockets to bind (Local guessing)
        self.port_range = 200        # Remote port guessing range (+/- this value)

        # Callbacks
        self.on_connection_established: Optional[Callable[[bytes], None]] = None
        self.on_connection_failed: Optional[Callable[[bytes], None]] = None
        self.on_data_received: Optional[Callable[[bytes, bytes], None]] = None

    def start(self):
        """Start the hole punching service"""
        if self.running:
            return

        self.running = True
        self.receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self.receive_thread.start()
        LoggerFactory.get_logger().info('P2P hole punching service started (N4 Strategy Enabled)')

    def stop(self):
        """Stop the hole punching service"""
        self.running = False
        with self.lock:
            for uid, conn in list(self.connections.items()):
                self._close_sockets(conn)
            self.connections.clear()
        LoggerFactory.get_logger().info('P2P hole punching service stopped')

    def _close_sockets(self, connection: P2PConnection):
        """Close all sockets in a connection"""
        if connection.sockets:
            for s in connection.sockets:
                try:
                    s.close()
                except:
                    pass
            connection.sockets.clear()

    def initiate_connection(self, uid: bytes, remote_ip: str, remote_port: int) -> bool:
        """
        Initiate a P2P connection using N4 strategy
        """
        try:
            connection = P2PConnection(uid, remote_ip, remote_port)

            # Create a pool of sockets (Brute-force local NAT mapping)
            for _ in range(self.socket_count):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

                # N4 Strategy: SO_REUSEADDR / SO_REUSEPORT for better binding
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    if hasattr(socket, "SO_REUSEPORT"):
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except Exception:
                    pass

                sock.bind(('0.0.0.0', 0)) # Let OS assign random port
                sock.setblocking(False)
                connection.sockets.append(sock)

            with self.lock:
                self.connections[uid] = connection

            LoggerFactory.get_logger().info(f'P2P init: UID {uid.hex()}, created {len(connection.sockets)} sockets, target base: {remote_ip}:{remote_port}')

            # Start hole punching process
            self._start_hole_punching(connection)
            return True

        except Exception as e:
            LoggerFactory.get_logger().error(f'Failed to initiate P2P connection: {e}')
            LoggerFactory.get_logger().error(traceback.format_exc())
            return False

    def _start_hole_punching(self, connection: P2PConnection):
        """
        Send punch packets to a range of ports from multiple local sockets
        """
        def punch_loop():
            uid = connection.uid
            retry_count = 0
            # N4: Aggressive punching for limited time
            max_retries = 20

            # Punch packet format
            punch_packet = b'PUNCH:' + uid

            while self.running and not connection.established and retry_count < max_retries:
                try:
                    # Strategy: Send to remote_base_port +/- offset
                    # Guessing symmetric NAT port assignment
                    target_ports = set()
                    target_ports.add(connection.remote_base_port)

                    # Add predicted ports
                    for i in range(1, self.port_range + 1):
                        target_ports.add(connection.remote_base_port + i)
                        target_ports.add(connection.remote_base_port - i)

                    # Send from ALL local sockets to ALL predicted remote ports
                    # This generates socket_count * (2*port_range + 1) packets per round
                    for sock in connection.sockets:
                        for port in target_ports:
                            if port <= 0 or port > 65535:
                                continue
                            try:
                                sock.sendto(punch_packet, (connection.remote_ip, port))
                            except OSError:
                                pass # Ignore send errors

                    if retry_count % 5 == 0:
                        LoggerFactory.get_logger().debug(f'Punching round {retry_count}: Sent burst to {len(target_ports)} ports')

                    time.sleep(0.5) # Wait a bit
                    retry_count += 1

                    if connection.established:
                        return

                except Exception as e:
                    LoggerFactory.get_logger().error(f'Error during hole punching loop: {e}')
                    LoggerFactory.get_logger().error(traceback.format_exc()) # 需要 import traceback
                    break

            if not connection.established:
                LoggerFactory.get_logger().warning(f'P2P hole punching failed after {retry_count} rounds: UID {uid.hex()}')
                self.close_connection(uid)
                if self.on_connection_failed:
                    self.on_connection_failed(uid)

        punch_thread = threading.Thread(target=punch_loop, daemon=True)
        punch_thread.start()

    def _receive_loop(self):
        """
        Receive loop handling all sockets
        """
        while self.running:
            try:
                # Gather all sockets from all connections
                all_sockets = []
                socket_to_conn_map = {}

                with self.lock:
                    for uid, conn in self.connections.items():
                        if conn.established:
                            if conn.active_socket:
                                all_sockets.append(conn.active_socket)
                                socket_to_conn_map[conn.active_socket] = conn
                        else:
                            for s in conn.sockets:
                                all_sockets.append(s)
                                socket_to_conn_map[s] = conn

                if not all_sockets:
                    time.sleep(0.1)
                    continue

                # Use select to wait for readable sockets
                readable, _, _ = select.select(all_sockets, [], [], 0.1)

                for sock in readable:
                    try:
                        data, addr = sock.recvfrom(65536)
                        conn = socket_to_conn_map.get(sock)

                        if not conn:
                            continue

                        # Logic for established connection
                        if conn.established:
                            if addr == conn.active_remote_addr:
                                if data.startswith(b'PUNCH:'): # Keep-alive / Handshake
                                    continue
                                if data.startswith(b'PUNCH_ACK:'):
                                    continue

                                conn.last_receive_time = time.time()
                                if self.on_data_received:
                                    self.on_data_received(conn.uid, data)
                            continue

                        # Logic for Handshake (Hole Punching Success)
                        if data.startswith(b'PUNCH:'):
                            punch_uid = data[6:10]
                            if punch_uid == conn.uid:
                                LoggerFactory.get_logger().info(f'WINNER! P2P Established: UID {conn.uid.hex()}. Route: Local {sock.getsockname()} <-> Remote {addr}')

                                # Send ACK immediately
                                sock.sendto(b'PUNCH_ACK:' + conn.uid, addr)

                                # Lock onto this socket and address
                                conn.active_socket = sock
                                conn.active_remote_addr = addr
                                conn.established = True

                                # Close other useless sockets to save resources
                                for s in conn.sockets:
                                    if s != sock:
                                        s.close()
                                conn.sockets = [sock] # Keep only the winner

                                if self.on_connection_established:
                                    self.on_connection_established(conn.uid)

                        elif data.startswith(b'PUNCH_ACK:'):
                            # We punched through, and they acknowledged
                            ack_uid = data[10:14]
                            if ack_uid == conn.uid:
                                LoggerFactory.get_logger().info(f'WINNER! Received ACK. P2P Established: UID {conn.uid.hex()}. Route: Local {sock.getsockname()} <-> Remote {addr}')
                                conn.active_socket = sock
                                conn.active_remote_addr = addr
                                conn.established = True

                                # Close others
                                for s in conn.sockets:
                                    if s != sock:
                                        s.close()
                                conn.sockets = [sock]

                                if self.on_connection_established:
                                    self.on_connection_established(conn.uid)

                    except OSError:
                        pass
                    except Exception as e:
                        LoggerFactory.get_logger().error(f"Error processing packet: {e}")

            except Exception as e:
                LoggerFactory.get_logger().error(f'Fatal error in receive loop: {e}')
                time.sleep(1)

    def send_data(self, uid: bytes, data: bytes) -> bool:
        """Send data using the established active socket"""
        with self.lock:
            connection = self.connections.get(uid)

        if not connection or not connection.established or not connection.active_socket:
            return False

        try:
            connection.active_socket.sendto(data, connection.active_remote_addr)
            connection.last_send_time = time.time()
            return True
        except Exception:
            return False

    def is_established(self, uid: bytes) -> bool:
        with self.lock:
            connection = self.connections.get(uid)
        return connection is not None and connection.established

    def close_connection(self, uid: bytes):
        with self.lock:
            connection = self.connections.pop(uid, None)

        if connection:
            self._close_sockets(connection)
            if connection.active_socket:
                try:
                    connection.active_socket.close()
                except:
                    pass
            LoggerFactory.get_logger().info(f'P2P connection closed: UID {uid.hex()}')