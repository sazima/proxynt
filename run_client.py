import asyncio
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import traceback
from optparse import OptionParser
from threading import Thread
from typing import List, Set, Dict
from urllib.parse import urlparse

import tornado
from tornado import ioloop

PROTOCOL_VERSION = 2

# Try to enable high-performance event loop (20-30% boost)
# Use winloop on Windows, uvloop on Linux/macOS
_UVLOOP_ENABLED = False
if sys.platform == 'win32':
    try:
        import winloop
        asyncio.set_event_loop_policy(winloop.EventLoopPolicy())
        _UVLOOP_ENABLED = True
    except ImportError:
        pass
else:
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        _UVLOOP_ENABLED = True
    except ImportError:
        pass

try:
    import snappy
    has_snappy = True
except ModuleNotFoundError:
    has_snappy = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.speed_limit import SpeedLimiter
from common.websocket import WebSocketException, ABNF
from common.n4_protocol import N4Packet
from client.udp_forward_client import UdpForwardClient
from client.heart_beat_task import HeatBeatTask
from client.tcp_forward_client import TcpForwardClient
from client.n4_tunnel_manager import N4TunnelManager, get_pair_key
from common import websocket
from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from constant.message_type_constnat import MessageTypeConstant
from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils
from entity.client_config_entity import ClientConfigEntity
from entity.message.message_entity import MessageEntity
from entity.message.push_config_entity import PushConfigEntity, ClientData
from entity.message.tcp_over_websocket_message import TcpOverWebsocketMessage
from exceptions.duplicated_name import DuplicatedName

tornado.ioloop.IOLoop.configure(tornado.ioloop.IOLoop.configured_class(), time_func=time.monotonic)

try:
    from common.websocket._logging import _logger
except (ImportError, ModuleNotFoundError):
    _logger = None

DEFAULT_CONFIG = './config_c.json'
DEFAULT_LOGGER_LEVEL = logging.INFO
NAME_TO_LEVEL = {
    'debug': logging.DEBUG,
    'info': logging.INFO,
    'warn': logging.WARN,
    'error': logging.ERROR
}

OPEN_CLOSE_LOCK = threading.Lock()
name_to_speed_limiter: Dict[str, SpeedLimiter] = {}
SERVER_P2P_SIGNAL_PORT = 19999  # N4 Signal Service UDP port


def get_config() -> ClientConfigEntity:
    parser = OptionParser(usage="""usage: %prog -c config_c.json

config_c.json example:
{
  "server": {
    "url": "ws://192.168.9.224:18888/websocket_path",
    "password": "helloworld"
  },
   "client_name": "ubuntu1",
  "client": [
    {
      "name": "ssh1",
      "remote_port": 12222,
      "local_port": 22,
      "local_ip": "127.0.0.1"
    }
  ],
  "log_file": "/var/log/nt/nt.log"
}
    """, version=SystemConstant.VERSION)
    parser.add_option("-c", "--config",
                      type='str',
                      dest='config',
                      default=DEFAULT_CONFIG,
                      help="config json file"
                      )
    parser.add_option("-l", "--level",
                      type='str',
                      dest='log_level',
                      default='info',
                      help="log level: debug, info, warn , error"
                      )
    (options, args) = parser.parse_args()
    log_level = options.log_level
    if log_level not in NAME_TO_LEVEL:
        print('invalid log level.')
        sys.exit()
    ContextUtils.set_log_level(NAME_TO_LEVEL[log_level])
    config_path = options.config
    with open(config_path, 'r') as rf:
        config_data: ClientConfigEntity = json.loads(rf.read())
    ContextUtils.set_config_file_path(os.path.abspath(config_path))
    ContextUtils.set_password(config_data['server']['password'])
    name_set: Set[str] = set()
    config_data.setdefault('client', [])
    for client in config_data['client']:
        if client['name'] in name_set:
            raise DuplicatedName()
        name_set.add(client['name'])
    return config_data


class WebsocketClient:
    def __init__(self, ws: websocket.WebSocketApp, tcp_forward_client, udp_forward_client, heart_beat_task, config_data: ClientConfigEntity):
        self.ws: websocket.WebSocketApp = ws
        self.ws.on_message = self.on_message
        self.ws.on_close = self.on_close
        self.ws.on_open = self.on_open
        self.forward_client: TcpForwardClient = tcp_forward_client
        self.udp_forward_client: UdpForwardClient = udp_forward_client
        self.heart_beat_task: HeatBeatTask = heart_beat_task
        self.config_data: ClientConfigEntity = config_data
        self.compress_support: bool = config_data['server']['compress']
        self.protocol_version: int = PROTOCOL_VERSION

        # P2P support
        self.public_ip: str = None
        self.public_port: int = None

        # Get server info for N4 signaling
        server_url = config_data['server']['url']
        parsed = urlparse(server_url)
        server_host = parsed.hostname
        signal_port = config_data.get('p2p_signal_port', SERVER_P2P_SIGNAL_PORT)
        client_name = config_data['client_name']

        # Initialize N4 Tunnel Manager
        self.tunnel_manager = N4TunnelManager(
            local_client_name=client_name,
            server_host=server_host,
            server_port=signal_port
        )
        self.tunnel_manager.set_ws_client(self)

        # Inject tunnel manager to TCP forward client for data routing
        self.forward_client.tunnel_manager = self.tunnel_manager

        # Callback for receiving P2P data (Tunnel -> Local)
        self.tunnel_manager.on_data_received = self._on_p2p_data_received
        self.tunnel_manager.on_tunnel_closed = self._on_tunnel_closed
        self.tunnel_manager.on_punch_failed = self._on_punch_failed

        self.tunnel_manager.start()

        # Pending punch sessions: session_id_hex -> {peer_name, ...}
        self.pending_sessions: Dict[str, dict] = {}

    def on_message(self, ws, message: bytes):
        try:
            message_data: MessageEntity = NatSerialization.loads(message, ContextUtils.get_password(), self.compress_support, self.protocol_version)
            self.heart_beat_task.set_recv_heart_beat_time(time.time())

            msg_type = message_data['type_']

            # Handle TCP messages
            if msg_type == MessageTypeConstant.WEBSOCKET_OVER_TCP:
                data: TcpOverWebsocketMessage = message_data['data']
                uid = data['uid']
                name = data['name']
                b = data['data']

                create_result = self.forward_client.create_socket(name, uid, data['ip_port'], name_to_speed_limiter.get(name))
                if create_result:
                    self.forward_client.send_by_uid(uid, b)

            # Handle TCP connection request
            elif msg_type == MessageTypeConstant.REQUEST_TO_CONNECT:
                data: TcpOverWebsocketMessage = message_data['data']
                uid = data['uid']
                name = data['name']

                # Register UID to tunnel_manager for P2P routing (if source_client provided)
                source_client = data.get('source_client')
                if source_client and self.tunnel_manager:
                    self.tunnel_manager.register_uid(uid, source_client)
                    LoggerFactory.get_logger().info(f'Registered UID {uid.hex()} to peer {source_client}')

                # 优先使用消息中携带的限速配置（C2C场景），否则使用本地配置
                speed_limit = data.get('speed_limit', 0.0)
                if speed_limit > 0:
                    LoggerFactory.get_logger().info(f'Using speed limit {speed_limit} for {name}')
                    speed_limiter = SpeedLimiter(speed_limit)
                else:
                    speed_limiter = name_to_speed_limiter.get(name)

                self.forward_client.create_socket(name, uid, data['ip_port'], speed_limiter)

            # Handle UDP messages
            elif msg_type == MessageTypeConstant.WEBSOCKET_OVER_UDP:
                data: TcpOverWebsocketMessage = message_data['data']
                uid = data['uid']
                name = data['name']
                b = data['data']
                create_result = self.udp_forward_client.create_udp_socket(name, uid, data['ip_port'], name_to_speed_limiter.get(name))
                if create_result:
                    self.udp_forward_client.send_by_uid(uid, b)

            # Handle UDP connection request
            elif msg_type == MessageTypeConstant.REQUEST_TO_CONNECT_UDP:
                data: TcpOverWebsocketMessage = message_data['data']
                uid = data['uid']
                name = data['name']

                # 优先使用消息中携带的限速配置（C2C场景），否则使用本地配置
                speed_limit = data.get('speed_limit', 0.0)
                if speed_limit > 0:
                    speed_limiter = SpeedLimiter(speed_limit)
                else:
                    speed_limiter = name_to_speed_limiter.get(name)

                self.udp_forward_client.create_udp_socket(name, uid, data['ip_port'], speed_limiter)

            # Handle Heartbeat / Config
            elif msg_type == MessageTypeConstant.PING:
                pass
            elif msg_type == MessageTypeConstant.PUSH_CONFIG:
                push_config: PushConfigEntity = message_data['data']
                self.public_ip = push_config.get('public_ip')
                self.public_port = push_config.get('public_port')

                for d in push_config['config_list']:
                    if d.get('speed_limit'):
                        name_to_speed_limiter[d['name']] = SpeedLimiter(d['speed_limit'])

                # Setup Listeners
                c2c_rules = push_config.get('client_to_client_rules', [])
                if c2c_rules:
                    LoggerFactory.get_logger().info(f'Received {len(c2c_rules)} C2C rules')
                    self.forward_client.setup_c2c_tcp_listeners(c2c_rules)
                    self.udp_forward_client.setup_c2c_udp_listeners(c2c_rules)

            # Handle P2P punch request from server
            elif msg_type == MessageTypeConstant.P2P_PUNCH_REQUEST:
                self._handle_punch_request(message_data['data'])

            # Handle P2P PEER_INFO (contains peer's actual UDP port from EXCHANGE)
            elif msg_type == MessageTypeConstant.P2P_PEER_INFO:
                self._handle_p2p_peer_info(message_data['data'])

        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def _handle_punch_request(self, data: dict):
        """Handle P2P_PUNCH_REQUEST from server - start EXCHANGE phase"""
        session_id_hex = data['session_id']
        peer_name = data['peer_name']

        LoggerFactory.get_logger().info(
            f'Received P2P_PUNCH_REQUEST: peer={peer_name}, session={session_id_hex}'
        )

        # Check if tunnel already exists
        if self.tunnel_manager.is_tunnel_established(peer_name):
            LoggerFactory.get_logger().info(f"Tunnel to {peer_name} already exists, skipping punch")
            return

        # Store pending session
        self.pending_sessions[session_id_hex] = {
            'peer_name': peer_name,
            'timestamp': time.time()
        }

        # Prepare socket pool and send EXCHANGE
        session_id = bytes.fromhex(session_id_hex)
        exchange_sock = self.tunnel_manager.prepare_punch(peer_name, session_id)

        if exchange_sock:
            self._send_exchange_packet(exchange_sock, session_id)

    def _send_exchange_packet(self, sock: socket.socket, session_id: bytes):
        """Send N4 EXCHANGE packet to server"""
        try:
            server_url = self.config_data['server']['url']
            parsed = urlparse(server_url)
            server_host = parsed.hostname
            signal_port = self.config_data.get('p2p_signal_port', SERVER_P2P_SIGNAL_PORT)

            # Create N4 EXCHANGE packet
            exchange_pkt = N4Packet.exchange(session_id)

            # Send 3 times to avoid packet loss
            for _ in range(3):
                try:
                    if sock.fileno() == -1:
                        return
                    sock.sendto(exchange_pkt, (server_host, signal_port))
                    time.sleep(0.05)
                except OSError as e:
                    if e.errno == 9:  # Bad file descriptor
                        return
                    LoggerFactory.get_logger().debug(f"Send EXCHANGE error: {e}")

            LoggerFactory.get_logger().info(
                f'Sent N4 EXCHANGE to {server_host}:{signal_port}, session={session_id.hex()}'
            )

        except Exception as e:
            LoggerFactory.get_logger().error(f'Failed to send EXCHANGE: {e}')

    def _handle_p2p_peer_info(self, data: dict):
        """Handle P2P_PEER_INFO from server - contains peer's actual UDP port"""
        session_id_hex = data['session_id']
        peer_name = data['peer_client']
        peer_ip = data['peer_ip']
        peer_port = int(data['peer_port'])

        LoggerFactory.get_logger().info(
            f'Received P2P_PEER_INFO: peer={peer_name}, addr={peer_ip}:{peer_port}'
        )

        # Remove pending session
        self.pending_sessions.pop(session_id_hex, None)

        # Start hole punching with peer info
        self.tunnel_manager.receive_peer_info(peer_name, peer_ip, peer_port)

    def _on_p2p_data_received(self, uid: bytes, data: bytes):
        """Callback when data comes from P2P Tunnel"""
        self.forward_client.send_by_uid(uid, data)

    def _on_tunnel_closed(self, peer_name: str):
        """Callback when tunnel is closed"""
        LoggerFactory.get_logger().info(f"Tunnel to {peer_name} closed")

    def _on_punch_failed(self, peer_name: str):
        """Callback when punch fails (timeout) - data will fall back to WebSocket"""
        LoggerFactory.get_logger().warning(
            f"Punch to {peer_name} failed (timeout), falling back to WebSocket"
        )

    def request_p2p_tunnel(self, target_client: str, uid: bytes):
        """
        Request P2P tunnel for a connection.
        Called by tcp_forward_client when C2C connection is initiated.
        """
        # Check if tunnel already established
        if self.tunnel_manager.is_tunnel_established(target_client):
            # Register UID and use existing tunnel
            self.tunnel_manager.register_uid(uid, target_client)
            LoggerFactory.get_logger().info(f"Reusing tunnel to {target_client} for UID {uid.hex()}")
            return True

        # Check if tunnel is being established
        if self.tunnel_manager.has_tunnel(target_client):
            # Register UID and wait for tunnel
            self.tunnel_manager.register_uid(uid, target_client)
            return True

        # Request server to initiate P2P punch
        self._send_punch_request(target_client)

        # Register UID for when tunnel is ready
        self.tunnel_manager.register_uid(uid, target_client)
        return True

    def _send_punch_request(self, target_client: str):
        """Send P2P_PUNCH_REQUEST to server"""
        try:
            msg: MessageEntity = {
                'type_': MessageTypeConstant.P2P_PUNCH_REQUEST,
                'data': {
                    'target_client': target_client
                }
            }
            if self.ws.sock and self.ws.sock.connected:
                self.ws.send(
                    NatSerialization.dumps(msg, ContextUtils.get_password(), self.compress_support, self.protocol_version),
                    websocket.ABNF.OPCODE_BINARY
                )
                LoggerFactory.get_logger().info(f"Sent P2P_PUNCH_REQUEST for {target_client}")
        except Exception as e:
            LoggerFactory.get_logger().error(f"Failed to send punch request: {e}")

    def on_open(self, ws):
        with OPEN_CLOSE_LOCK:
            try:
                LoggerFactory.get_logger().info('WS Open: Resetting clients...')
                self.heart_beat_task.is_running = False
                self.forward_client.close()
                self.udp_forward_client.close()

                # Restart tunnel manager
                if self.tunnel_manager:
                    self.tunnel_manager.stop()
                    self.tunnel_manager.start()

                self.forward_client.update_websocket(ws)
                self.udp_forward_client.update_websocket(ws)

                LoggerFactory.get_logger().info('Sending config...')
                push_client_data: List[ClientData] = self.config_data['client']
                client_name = self.config_data.get('client_name', socket.gethostname())

                for item in push_client_data:
                    if 'protocol' not in item:
                        item['protocol'] = 'tcp'

                push_configs: PushConfigEntity = {
                    'key': ContextUtils.get_password(),
                    'config_list': push_client_data,
                    "client_name": client_name,
                    'version': SystemConstant.VERSION,
                    'p2p_supported': True
                }
                message: MessageEntity = {
                    'type_': MessageTypeConstant.PUSH_CONFIG,
                    'data': push_configs
                }
                self.heart_beat_task.set_recv_heart_beat_time(time.time())

                ws.send(NatSerialization.dumps(message, ContextUtils.get_password(), self.compress_support, self.protocol_version), websocket.ABNF.OPCODE_BINARY)
                self.forward_client.set_running(True)
                self.udp_forward_client.set_running(True)
                self.heart_beat_task.is_running = True

            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())

    def on_error(self, ws, error):
        LoggerFactory.get_logger().error(f'WS Error: {error}')

    def on_close(self, ws, a, b):
        with OPEN_CLOSE_LOCK:
            LoggerFactory.get_logger().info(f'WS Closed: {a}, {b}')
            self.heart_beat_task.is_running = False
            self.forward_client.close()
            # Stop tunnel manager
            if self.tunnel_manager:
                self.tunnel_manager.stop()


def signal_handler(sig, frame):
    print('You pressed Ctrl+C!')
    os._exit(0)


def run_client(ws: websocket.WebSocketApp):
    while True:
        try:
            ws.run_forever()
        except WebSocketException as e:
            LoggerFactory.get_logger().warn('WebSocketException: {}'.format(e))
            time.sleep(5)
        except Exception as e:
            LoggerFactory.get_logger().error(traceback.format_exc())
            time.sleep(5)
            continue
        LoggerFactory.get_logger().info(f'Reconnecting in 2 seconds...')
        time.sleep(2)


def main():
    print('github: ', SystemConstant.GITHUB)
    if _UVLOOP_ENABLED:
        print('high-performance event loop enabled (uvloop/winloop)')

    from common.encrypt_utils import EncryptUtils
    if not EncryptUtils.is_xxhash_available():
        print('Warning: xxhash not installed, please run: pip install xxhash')
        sys.exit(1)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    config_data = get_config()
    signal.signal(signal.SIGINT, signal_handler)
    websocket.setdefaulttimeout(3)
    server_config = config_data['server']
    if not server_config['password']:
        raise Exception('Password cannot be empty')

    log_path = config_data.get('log_file')
    ContextUtils.set_log_file(log_path)
    ContextUtils.set_nonce_to_time({})

    url = server_config.get('url', '')
    if not url:
        url = ''
        if server_config['https']:
            url += 'wss://'
        else:
            url += 'ws://'
        url += f"{server_config['host']}:{str(server_config['port'])}{server_config['path']}"

    config_data['server'].setdefault('compress', False)
    compress_support = config_data['server']['compress']
    if compress_support and not has_snappy:
        raise Exception('snappy is not installed')

    # Get protocol version from config, default to 2 for new clients
    config_data['server'].setdefault('protocol_version', 2)
    protocol_version = config_data['server']['protocol_version']

    LoggerFactory.get_logger().info(f'Connecting to {url}')

    # Add URL parameters
    if compress_support:
        sep = '&' if '?' in url else '?'
        url += sep + 'c=' + json.dumps(compress_support)

    # Add protocol version to URL
    sep = '&' if '?' in url else '?'
    url += sep + 'v=' + str(protocol_version)

    ws = websocket.WebSocketApp(url)

    # Init Clients
    forward_client = TcpForwardClient(ws, compress_support, protocol_version)
    udp_forward_client = UdpForwardClient(ws, compress_support, protocol_version)
    heart_beat_task = HeatBeatTask(ws, SystemConstant.HEART_BEAT_INTERVAL, protocol_version)

    # Init Controller
    WebsocketClient(ws, forward_client, udp_forward_client, heart_beat_task, config_data)

    LoggerFactory.get_logger().info('Client started')

    Thread(target=run_client, args=(ws,)).start()
    Thread(target=heart_beat_task.run).start()
    Thread(target=forward_client.start_forward).start()

    ioloop.IOLoop.current().start()


if __name__ == '__main__':
    main()
