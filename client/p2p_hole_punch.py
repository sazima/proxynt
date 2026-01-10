"""
UDP Hole Punching Module for P2P Connection
"""
import os
import socket
import threading
import time
import traceback
from typing import Dict, Optional, Callable

from common.logger_factory import LoggerFactory
from constant.system_constant import SystemConstant


class P2PConnection:
    """Represents a P2P connection"""
    def __init__(self, uid: bytes, local_socket: socket.socket, remote_addr: tuple):
        self.uid = uid
        self.local_socket = local_socket
        self.remote_addr = remote_addr  # (ip, port)
        self.established = False
        self.last_receive_time = 0
        self.last_send_time = 0


class P2PHolePunch:
    """UDP Hole Punching Manager"""

    def __init__(self):
        self.connections: Dict[bytes, P2PConnection] = {}
        self.lock = threading.Lock()
        self.running = False
        self.receive_thread: Optional[threading.Thread] = None

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
        LoggerFactory.get_logger().info('P2P hole punching service started')

    def stop(self):
        """Stop the hole punching service"""
        self.running = False
        with self.lock:
            for uid, conn in list(self.connections.items()):
                try:
                    conn.local_socket.close()
                except Exception:
                    pass
            self.connections.clear()
        LoggerFactory.get_logger().info('P2P hole punching service stopped')

    def initiate_connection(self, uid: bytes, remote_ip: str, remote_port: int, local_port: int = 0) -> bool:
        """
        Initiate a P2P connection

        :param uid: Unique connection ID
        :param remote_ip: Remote peer's public IP
        :param remote_port: Remote peer's public port
        :param local_port: Local port to bind (0 for random)
        :return: True if initiated successfully
        """
        try:
            # Create UDP socket
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Bind to local port
            if local_port > 0:
                udp_socket.bind(('0.0.0.0', local_port))
            else:
                udp_socket.bind(('0.0.0.0', 0))  # Let OS assign port

            local_addr = udp_socket.getsockname()
            LoggerFactory.get_logger().info(f'P2P connection initiated: UID {uid.hex()}, local port: {local_addr[1]}, remote: {remote_ip}:{remote_port}')

            # Create connection object
            remote_addr = (remote_ip, remote_port)
            connection = P2PConnection(uid, udp_socket, remote_addr)

            with self.lock:
                self.connections[uid] = connection

            # Start hole punching process
            self._start_hole_punching(connection)
            return True

        except Exception as e:
            LoggerFactory.get_logger().error(f'Failed to initiate P2P connection: {e}')
            LoggerFactory.get_logger().error(traceback.format_exc())
            return False

    def _start_hole_punching(self, connection: P2PConnection):
        """
        Start the hole punching process
        Send periodic punch packets to remote peer
        """
        def punch_loop():
            uid = connection.uid
            sock = connection.local_socket
            remote_addr = connection.remote_addr
            retry_count = 0
            max_retries = SystemConstant.P2P_MAX_RETRY

            # Punch packet format: "PUNCH:" + UID (to distinguish from data packets)
            punch_packet = b'PUNCH:' + uid

            while self.running and not connection.established and retry_count < max_retries:
                try:
                    # Send punch packet
                    sock.sendto(punch_packet, remote_addr)
                    connection.last_send_time = time.time()
                    LoggerFactory.get_logger().debug(f'Sent punch packet to {remote_addr}, attempt {retry_count + 1}/{max_retries}')

                    # Wait for response
                    time.sleep(1)
                    retry_count += 1

                    # Check if connection established (by receive loop)
                    if connection.established:
                        LoggerFactory.get_logger().info(f'P2P connection established: UID {uid.hex()}, remote: {remote_addr}')
                        if self.on_connection_established:
                            self.on_connection_established(uid)
                        return

                except Exception as e:
                    LoggerFactory.get_logger().error(f'Error during hole punching: {e}')
                    break

            # Timeout or failed
            if not connection.established:
                LoggerFactory.get_logger().warning(f'P2P hole punching failed: UID {uid.hex()}, remote: {remote_addr}')
                self.close_connection(uid)
                if self.on_connection_failed:
                    self.on_connection_failed(uid)

        # Start punch thread
        punch_thread = threading.Thread(target=punch_loop, daemon=True)
        punch_thread.start()

    def _receive_loop(self):
        """Receive loop for all P2P connections"""
        while self.running:
            try:
                # Check all connections for incoming data
                with self.lock:
                    connections = list(self.connections.values())

                for conn in connections:
                    try:
                        conn.local_socket.settimeout(0.1)  # Non-blocking with short timeout
                        data, addr = conn.local_socket.recvfrom(65536)

                        if not data:
                            continue

                        # Check if this is a punch packet
                        if data.startswith(b'PUNCH:'):
                            punch_uid = data[6:10]  # Extract UID from punch packet
                            if punch_uid == conn.uid:
                                # Received punch packet from remote peer
                                LoggerFactory.get_logger().info(f'Received punch packet from {addr} for UID {conn.uid.hex()}')

                                # Send punch reply
                                reply_packet = b'PUNCH_ACK:' + conn.uid
                                conn.local_socket.sendto(reply_packet, addr)

                                # Update remote address (might have changed due to NAT)
                                conn.remote_addr = addr
                                conn.established = True
                                conn.last_receive_time = time.time()

                        elif data.startswith(b'PUNCH_ACK:'):
                            # Received punch acknowledgment
                            LoggerFactory.get_logger().info(f'Received punch ACK from {addr} for UID {conn.uid.hex()}')
                            conn.remote_addr = addr
                            conn.established = True
                            conn.last_receive_time = time.time()

                        else:
                            # Regular data packet
                            if conn.established:
                                conn.last_receive_time = time.time()
                                if self.on_data_received:
                                    self.on_data_received(conn.uid, data)
                            else:
                                LoggerFactory.get_logger().warning(f'Received data on unestablished connection: UID {conn.uid.hex()}')

                    except socket.timeout:
                        continue
                    except OSError:
                        # Socket might be closed
                        continue
                    except Exception as e:
                        LoggerFactory.get_logger().error(f'Error in receive loop: {e}')

                # Small sleep to prevent CPU spinning
                time.sleep(0.01)

            except Exception as e:
                LoggerFactory.get_logger().error(f'Fatal error in P2P receive loop: {e}')
                LoggerFactory.get_logger().error(traceback.format_exc())
                time.sleep(1)

    def send_data(self, uid: bytes, data: bytes) -> bool:
        """
        Send data over P2P connection

        :param uid: Connection UID
        :param data: Data to send
        :return: True if sent successfully
        """
        with self.lock:
            connection = self.connections.get(uid)

        if not connection:
            LoggerFactory.get_logger().warning(f'P2P connection not found: UID {uid.hex()}')
            return False

        if not connection.established:
            LoggerFactory.get_logger().warning(f'P2P connection not established: UID {uid.hex()}')
            return False

        try:
            connection.local_socket.sendto(data, connection.remote_addr)
            connection.last_send_time = time.time()
            return True
        except Exception as e:
            LoggerFactory.get_logger().error(f'Failed to send P2P data: {e}')
            return False

    def is_established(self, uid: bytes) -> bool:
        """Check if P2P connection is established"""
        with self.lock:
            connection = self.connections.get(uid)
        return connection is not None and connection.established

    def close_connection(self, uid: bytes):
        """Close a P2P connection"""
        with self.lock:
            connection = self.connections.pop(uid, None)

        if connection:
            try:
                connection.local_socket.close()
            except Exception:
                pass
            LoggerFactory.get_logger().info(f'P2P connection closed: UID {uid.hex()}')
