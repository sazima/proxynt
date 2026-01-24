"""
N4 Signal Service for P2P Hole Punching Coordination

Listens for EXCHANGE packets from clients and coordinates hole punching
by sending peer info to both parties when ready.
"""
import socket
import threading
import time
import traceback
from functools import partial
from typing import Dict, Optional, Tuple, List

from common.logger_factory import LoggerFactory
from common.n4_protocol import N4Packet


class PunchSession:
    """Represents a pending hole punching session between two clients"""

    def __init__(self, session_id: bytes, client_a: str, client_b: str):
        self.session_id = session_id
        self.client_a = client_a
        self.client_b = client_b

        # Addresses received from EXCHANGE
        self.addr_a: Optional[Tuple[str, int]] = None
        self.addr_b: Optional[Tuple[str, int]] = None

        self.timestamp = time.time()

    def is_ready(self) -> bool:
        """Check if both clients have sent EXCHANGE"""
        return self.addr_a is not None and self.addr_b is not None


class N4SignalService:
    """
    N4-based signaling service for P2P hole punching coordination.

    Uses N4 binary protocol for UDP signaling:
    - Receives EXCHANGE packets from clients
    - Records their public addresses (NAT-mapped)
    - Sends PEER_INFO to both when ready
    """

    _instance = None

    def __init__(self, port: int):
        self.port = port
        self.running = False
        self.sock: Optional[socket.socket] = None
        self.thread: Optional[threading.Thread] = None

        # Pending sessions: pair_key -> PunchSession
        # pair_key = (min(client_a, client_b), max(client_a, client_b))
        self.pending_sessions: Dict[Tuple[str, str], PunchSession] = {}
        self.lock = threading.Lock()

        # Session ID counter
        self._session_counter = 0

        # Tornado IOLoop reference (for thread-safe WebSocket messages)
        self.tornado_loop = None

        # Cleanup settings
        self.session_timeout = 60  # seconds

    @classmethod
    def get_instance(cls, port: int = 19999) -> 'N4SignalService':
        """Get singleton instance"""
        if cls._instance is None:
            cls._instance = cls(port)
        return cls._instance

    def set_tornado_loop(self, loop):
        """Set Tornado IOLoop reference"""
        self.tornado_loop = loop

    def start(self):
        """Start UDP listener for N4 EXCHANGE packets"""
        if self.running:
            LoggerFactory.get_logger().warning('N4 Signal Service already running')
            return

        # Get tornado loop if not set
        if not self.tornado_loop:
            try:
                import tornado.ioloop
                self.tornado_loop = tornado.ioloop.IOLoop.current()
            except:
                LoggerFactory.get_logger().warning('Failed to get Tornado IOLoop')

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                if hasattr(socket, "SO_REUSEPORT"):
                    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except:
                pass

            self.sock.bind(('0.0.0.0', self.port))
            self.running = True

            # Start receive thread
            self.thread = threading.Thread(target=self._receive_loop, daemon=True)
            self.thread.start()

            # Start cleanup thread
            cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
            cleanup_thread.start()

            LoggerFactory.get_logger().info(f'N4 Signal Service started on UDP port {self.port}')
        except Exception as e:
            LoggerFactory.get_logger().error(f'Failed to start N4 Signal Service: {e}')
            raise

    def stop(self):
        """Stop the service"""
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
        LoggerFactory.get_logger().info('N4 Signal Service stopped')

    def request_punch(self, client_a: str, client_b: str) -> bytes:
        """
        Request hole punching between two clients.
        Returns session_id (6 bytes) for the punch session.

        Called by websocket_handler when P2P is needed.
        """
        pair_key = (min(client_a, client_b), max(client_a, client_b))

        with self.lock:
            # Check if session already exists
            existing = self.pending_sessions.get(pair_key)
            if existing:
                LoggerFactory.get_logger().info(
                    f"Reusing existing session for {client_a} <-> {client_b}"
                )
                return existing.session_id

            # Generate new session ID (6 bytes)
            self._session_counter += 1
            session_id = self._session_counter.to_bytes(6, 'big')

            # Create new session
            session = PunchSession(session_id, client_a, client_b)
            self.pending_sessions[pair_key] = session

            LoggerFactory.get_logger().info(
                f"Created punch session {session_id.hex()} for {client_a} <-> {client_b}"
            )

            return session_id

    def _receive_loop(self):
        """Receive N4 EXCHANGE packets"""
        LoggerFactory.get_logger().info('N4 Signal receive loop started')

        while self.running:
            try:
                self.sock.settimeout(1.0)

                try:
                    data, addr = self.sock.recvfrom(1024)
                except socket.timeout:
                    continue

                # Try to decode as N4 EXCHANGE packet
                if len(data) == N4Packet.SIZE:
                    session_id = N4Packet.dec_exchange(data)
                    if session_id is not None:
                        self._handle_exchange(session_id, addr)

            except Exception as e:
                if self.running:
                    LoggerFactory.get_logger().error(f'Error in N4 receive loop: {e}')
                time.sleep(0.1)

    def _handle_exchange(self, session_id: bytes, addr: Tuple[str, int]):
        """Handle received EXCHANGE packet"""
        LoggerFactory.get_logger().info(
            f"Received EXCHANGE from {addr[0]}:{addr[1]}, session={session_id.hex()}"
        )

        with self.lock:
            # Find session by session_id
            session = None
            for s in self.pending_sessions.values():
                if s.session_id == session_id:
                    session = s
                    break

            if not session:
                LoggerFactory.get_logger().warning(
                    f"No session found for session_id {session_id.hex()}"
                )
                return

            # Determine which client this is from
            # We need to match by checking WebSocket handler's public IP
            from server.websocket_handler import MyWebSocketaHandler

            handler_a = MyWebSocketaHandler.client_name_to_handler.get(session.client_a)
            handler_b = MyWebSocketaHandler.client_name_to_handler.get(session.client_b)

            if handler_a and handler_a.public_ip == addr[0]:
                session.addr_a = addr
                LoggerFactory.get_logger().info(
                    f"Session {session_id.hex()}: {session.client_a} at {addr}"
                )
            elif handler_b and handler_b.public_ip == addr[0]:
                session.addr_b = addr
                LoggerFactory.get_logger().info(
                    f"Session {session_id.hex()}: {session.client_b} at {addr}"
                )
            else:
                # Fallback: assign to first empty slot
                if session.addr_a is None:
                    session.addr_a = addr
                    LoggerFactory.get_logger().info(
                        f"Session {session_id.hex()}: slot A = {addr}"
                    )
                elif session.addr_b is None:
                    session.addr_b = addr
                    LoggerFactory.get_logger().info(
                        f"Session {session_id.hex()}: slot B = {addr}"
                    )

            # Check if both are ready
            if session.is_ready():
                self._notify_peers(session)

                # Remove session
                pair_key = (min(session.client_a, session.client_b),
                           max(session.client_a, session.client_b))
                self.pending_sessions.pop(pair_key, None)

    def _notify_peers(self, session: PunchSession):
        """Send PEER_INFO to both clients"""
        LoggerFactory.get_logger().info(
            f"Session {session.session_id.hex()} ready: "
            f"{session.client_a}@{session.addr_a} <-> {session.client_b}@{session.addr_b}"
        )

        try:
            from server.websocket_handler import MyWebSocketaHandler
            from constant.message_type_constnat import MessageTypeConstant
            from common.nat_serialization import NatSerialization
            from context.context_utils import ContextUtils

            handler_a = MyWebSocketaHandler.client_name_to_handler.get(session.client_a)
            handler_b = MyWebSocketaHandler.client_name_to_handler.get(session.client_b)

            if not handler_a or not handler_b:
                LoggerFactory.get_logger().warning('Handler not found for peer notification')
                return

            # Message to client_a (with client_b's address)
            msg_to_a = {
                'type_': MessageTypeConstant.P2P_PEER_INFO,
                'data': {
                    'session_id': session.session_id.hex(),
                    'peer_client': session.client_b,
                    'peer_ip': session.addr_b[0],
                    'peer_port': session.addr_b[1]
                }
            }

            # Message to client_b (with client_a's address)
            msg_to_b = {
                'type_': MessageTypeConstant.P2P_PEER_INFO,
                'data': {
                    'session_id': session.session_id.hex(),
                    'peer_client': session.client_a,
                    'peer_ip': session.addr_a[0],
                    'peer_port': session.addr_a[1]
                }
            }

            # Serialize messages
            password = ContextUtils.get_password()
            data_to_a = NatSerialization.dumps(msg_to_a, password, handler_a.compress_support, handler_a.protocol_version)
            data_to_b = NatSerialization.dumps(msg_to_b, password, handler_b.compress_support, handler_b.protocol_version)

            # Send via Tornado (thread-safe)
            if self.tornado_loop:
                self.tornado_loop.add_callback(
                    partial(handler_a.write_message, data_to_a, True)
                )
                self.tornado_loop.add_callback(
                    partial(handler_b.write_message, data_to_b, True)
                )

            LoggerFactory.get_logger().info(
                f"Sent P2P_PEER_INFO to {session.client_a} and {session.client_b}"
            )

        except Exception as e:
            LoggerFactory.get_logger().error(f'Error notifying peers: {e}')
            LoggerFactory.get_logger().error(traceback.format_exc())

    def _cleanup_loop(self):
        """Cleanup expired sessions"""
        while self.running:
            time.sleep(30)

            try:
                now = time.time()
                with self.lock:
                    expired = [
                        key for key, session in self.pending_sessions.items()
                        if now - session.timestamp > self.session_timeout
                    ]

                    for key in expired:
                        del self.pending_sessions[key]
                        LoggerFactory.get_logger().debug(f'Cleaned up expired session: {key}')

            except Exception as e:
                LoggerFactory.get_logger().error(f'Error in cleanup loop: {e}')
