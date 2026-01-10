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

import tornado

# Try to enable uvloop for performance boost (20-30%)
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    _UVLOOP_ENABLED = True
except ImportError:
    _UVLOOP_ENABLED = False


try:
    import snappy
    has_snappy = True
except ModuleNotFoundError:
    has_snappy = False

from tornado import ioloop

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.speed_limit import SpeedLimiter
from common.websocket import WebSocketException, ABNF, WebSocketConnectionClosedException
from client.udp_forward_client import UdpForwardClient
from client.heart_beat_task import HeatBeatTask
from client.tcp_forward_client import TcpForwardClient
from client.p2p_hole_punch import P2PHolePunch
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
        self.udp_forward_client: UdpForwardClient = udp_forward_client  # UDP forward client
        self.heart_beat_task: HeatBeatTask = heart_beat_task
        self.config_data: ClientConfigEntity = config_data
        self.compress_support: bool = config_data['server']['compress']

        # P2P support
        self.public_ip: str = None  # Client's public IP (received from server)
        self.public_port: int = None  # Client's public port (received from server)
        self.p2p_hole_punch: P2PHolePunch = None  # P2P hole punching manager
        self.p2p_connections: Dict[bytes, dict] = {}  # uid -> {role, peer_client, service_name, ip_port}

        # Initialize P2P module (always initialize, enable/disable per rule)
        self.p2p_hole_punch = P2PHolePunch()
        self.p2p_hole_punch.on_connection_established = self._on_p2p_established
        self.p2p_hole_punch.on_connection_failed = self._on_p2p_failed
        self.p2p_hole_punch.on_data_received = self._on_p2p_data_received
        self.p2p_hole_punch.start()
        LoggerFactory.get_logger().info('P2P hole punching service started')


    def on_message(self, ws, message: bytes):
        try:
            message_data: MessageEntity = NatSerialization.loads(message, ContextUtils.get_password(), self.compress_support)
            start_time = time.time()
            time_ = message_data['type_']
            self.heart_beat_task.set_recv_heart_beat_time(time.time())  # 所有的消息等更新心跳时间
            # Handle TCP messages
            if message_data['type_'] == MessageTypeConstant.WEBSOCKET_OVER_TCP:
                data: TcpOverWebsocketMessage = message_data['data']
                uid = data['uid']
                name = data['name']
                b = data['data']
                create_result = self.forward_client.create_socket(name, uid, data['ip_port'], name_to_speed_limiter.get(name))
                if create_result:
                    self.forward_client.send_by_uid(uid, b)

            # Handle TCP connection request
            elif message_data['type_'] == MessageTypeConstant.REQUEST_TO_CONNECT:
                data: TcpOverWebsocketMessage = message_data['data']
                uid = data['uid']
                name = data['name']
                b = data['data']
                self.forward_client.create_socket(name, uid, data['ip_port'], name_to_speed_limiter.get(name))

            # Handle UDP messages
            elif message_data['type_'] == MessageTypeConstant.WEBSOCKET_OVER_UDP:
                data: TcpOverWebsocketMessage = message_data['data']
                uid = data['uid']
                name = data['name']
                b = data['data']
                create_result = self.udp_forward_client.create_udp_socket(name, uid, data['ip_port'], name_to_speed_limiter.get(name))
                if create_result:
                    self.udp_forward_client.send_by_uid(uid, b)

            # Handle UDP connection request
            elif message_data['type_'] == MessageTypeConstant.REQUEST_TO_CONNECT_UDP:
                data: TcpOverWebsocketMessage = message_data['data']
                uid = data['uid']
                name = data['name']
                b = data['data']
                self.udp_forward_client.create_udp_socket(name, uid, data['ip_port'], name_to_speed_limiter.get(name))

            # Handle heartbeat and config push
            elif message_data['type_'] == MessageTypeConstant.PING:
                self.heart_beat_task.set_recv_heart_beat_time(time.time())
            elif message_data['type_'] == MessageTypeConstant.PUSH_CONFIG:
                push_config: PushConfigEntity = message_data['data']

                # Receive public IP and port from server (for P2P)
                self.public_ip = push_config.get('public_ip')
                self.public_port = push_config.get('public_port')
                if self.public_ip:
                    LoggerFactory.get_logger().info(f'P2P supported, public address: {self.public_ip}:{self.public_port}')

                for d in push_config['config_list']:
                    # Set speed limiter
                    if d.get('speed_limit'):
                        name_to_speed_limiter[d['name']] = SpeedLimiter(d['speed_limit'])

                # Handle C2C rules (if exists)
                c2c_rules = push_config.get('client_to_client_rules', [])
                if c2c_rules:
                    LoggerFactory.get_logger().info(f'Received {len(c2c_rules)} C2C rules, setting up listeners')
                    # Setup C2C TCP listeners
                    self.forward_client.setup_c2c_tcp_listeners(c2c_rules)
                    # Setup C2C UDP listeners
                    self.udp_forward_client.setup_c2c_udp_listeners(c2c_rules)

            # Handle P2P OFFER
            elif message_data['type_'] == MessageTypeConstant.P2P_OFFER:
                self._handle_p2p_offer(message_data['data'])

            # Handle P2P SUCCESS (from peer)
            elif message_data['type_'] == MessageTypeConstant.P2P_SUCCESS:
                self._handle_p2p_success(message_data['data'])

            # Handle P2P FAILED (from peer)
            elif message_data['type_'] == MessageTypeConstant.P2P_FAILED:
                self._handle_p2p_failed(message_data['data'])

        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
        # LoggerFactory.get_logger().debug(f'on message {time_} cost time {time.time() - start_time}')

    def on_open(self, ws):
        with OPEN_CLOSE_LOCK:
            try:
                LoggerFactory.get_logger().info('close before open..')
                self.heart_beat_task.is_running = False
                self.forward_client.close()
                self.udp_forward_client.close()  # Close UDP client

                # Update websocket reference (fix C2C listener threads using old connection)
                self.forward_client.update_websocket(ws)
                self.udp_forward_client.update_websocket(ws)

                LoggerFactory.get_logger().info('open success')
                push_client_data: List[ClientData] = self.config_data['client']
                client_name = self.config_data.get('client_name', socket.gethostname())

                # Ensure each config item has protocol field, default to 'tcp'
                for item in push_client_data:
                    if 'protocol' not in item:
                        item['protocol'] = 'tcp'

                push_configs: PushConfigEntity = {
                    'key': ContextUtils.get_password(),
                    'config_list': push_client_data,
                    "client_name": client_name,
                    'version': SystemConstant.VERSION,
                    'p2p_supported': True  # Always declare P2P support (enable/disable per rule)
                }
                message: MessageEntity = {
                    'type_': MessageTypeConstant.PUSH_CONFIG,
                    'data': push_configs
                }
                self.heart_beat_task.set_recv_heart_beat_time(time.time())

                ws.send(NatSerialization.dumps(message, ContextUtils.get_password(), self.compress_support), websocket.ABNF.OPCODE_BINARY)
                self.forward_client.set_running(True)
                self.udp_forward_client.set_running(True)  # Start UDP client
                self.heart_beat_task.is_running = True
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())

    def _handle_p2p_offer(self, data: dict):
        """Handle P2P OFFER message from server"""
        if not self.p2p_hole_punch:
            LoggerFactory.get_logger().error('P2P module not initialized, ignoring P2P_OFFER')
            return

        # Convert uid from hex string to bytes
        uid_hex = data['uid']
        uid = bytes.fromhex(uid_hex)
        role = data['role']  # 'initiator' or 'responder'
        peer_client = data['peer_client']
        peer_public_ip = data['peer_public_ip']
        peer_public_port = data['peer_public_port']
        service_name = data['service_name']
        ip_port = data['ip_port']

        LoggerFactory.get_logger().info(f'Received P2P_OFFER: role={role}, peer={peer_client}, peer_addr={peer_public_ip}:{peer_public_port}')

        # Store connection info
        self.p2p_connections[uid] = {
            'role': role,
            'peer_client': peer_client,
            'service_name': service_name,
            'ip_port': ip_port,
            'peer_public_ip': peer_public_ip,
            'peer_public_port': peer_public_port
        }

        # Initiate P2P connection
        success = self.p2p_hole_punch.initiate_connection(uid, peer_public_ip, peer_public_port)
        if not success:
            LoggerFactory.get_logger().error(f'Failed to initiate P2P connection for UID {uid.hex()}')
            # Notify server about failure (will use WebSocket fallback)
            self._send_p2p_failed(uid)

    def _on_p2p_established(self, uid: bytes):
        """Callback when P2P connection is established"""
        conn_info = self.p2p_connections.get(uid)
        if not conn_info:
            LoggerFactory.get_logger().warning(f'P2P connection established but no info found for UID {uid.hex()}')
            return

        LoggerFactory.get_logger().info(f'P2P connection established: UID {uid.hex()}, peer={conn_info["peer_client"]}')

        # Notify server and peer about success
        self._send_p2p_success(uid)

        # If this is the responder, also need to connect to local service
        if conn_info['role'] == 'responder':
            # Create local socket connection
            ip_port = conn_info['ip_port']
            service_name = conn_info['service_name']
            # Tell forward client to create socket for this UID (but mark it as P2P)
            # For now, we'll let the normal flow handle local connection
            LoggerFactory.get_logger().info(f'P2P responder ready for UID {uid.hex()}')

    def _on_p2p_failed(self, uid: bytes):
        """Callback when P2P connection failed"""
        conn_info = self.p2p_connections.get(uid)
        if conn_info:
            LoggerFactory.get_logger().warning(f'P2P connection failed: UID {uid.hex()}, peer={conn_info["peer_client"]}, falling back to WebSocket relay')
        else:
            LoggerFactory.get_logger().warning(f'P2P connection failed for UID {uid.hex()}')

        # Notify server about failure (server will handle WebSocket fallback)
        self._send_p2p_failed(uid)

        # Clean up
        self.p2p_connections.pop(uid, None)

    def _on_p2p_data_received(self, uid: bytes, data: bytes):
        """Callback when P2P data is received"""
        conn_info = self.p2p_connections.get(uid)
        if not conn_info:
            LoggerFactory.get_logger().warning(f'P2P data received but no connection info for UID {uid.hex()}')
            return

        # Forward data to local service
        if conn_info['role'] == 'responder':
            # Responder: forward to local service
            self.forward_client.send_by_uid(uid, data)
        else:
            # Initiator: forward to local application
            self.forward_client.send_by_uid(uid, data)

    def _handle_p2p_success(self, data: dict):
        """Handle P2P_SUCCESS message from peer (via server)"""
        uid_hex = data.get('uid')
        if uid_hex:
            uid = bytes.fromhex(uid_hex) if isinstance(uid_hex, str) else uid_hex
            LoggerFactory.get_logger().info(f'Peer confirmed P2P success for UID {uid.hex()}')

    def _handle_p2p_failed(self, data: dict):
        """Handle P2P_FAILED message from peer (via server)"""
        uid_hex = data.get('uid')
        if uid_hex:
            uid = bytes.fromhex(uid_hex) if isinstance(uid_hex, str) else uid_hex
            LoggerFactory.get_logger().info(f'Peer reported P2P failure for UID {uid.hex()}, using WebSocket fallback')
            # Clean up local P2P connection if exists
            if self.p2p_hole_punch:
                self.p2p_hole_punch.close_connection(uid)
            self.p2p_connections.pop(uid, None)

    def _send_p2p_success(self, uid: bytes):
        """Send P2P_SUCCESS message to server"""
        try:
            message: MessageEntity = {
                'type_': MessageTypeConstant.P2P_SUCCESS,
                'data': {'uid': uid.hex()}  # Convert bytes to hex string for JSON serialization
            }
            self.ws.send(NatSerialization.dumps(message, ContextUtils.get_password(), self.compress_support), websocket.ABNF.OPCODE_BINARY)
            LoggerFactory.get_logger().debug(f'Sent P2P_SUCCESS for UID {uid.hex()}')
        except Exception as e:
            LoggerFactory.get_logger().error(f'Failed to send P2P_SUCCESS: {e}')

    def _send_p2p_failed(self, uid: bytes):
        """Send P2P_FAILED message to server"""
        try:
            message: MessageEntity = {
                'type_': MessageTypeConstant.P2P_FAILED,
                'data': {'uid': uid.hex()}  # Convert bytes to hex string for JSON serialization
            }
            self.ws.send(NatSerialization.dumps(message, ContextUtils.get_password(), self.compress_support), websocket.ABNF.OPCODE_BINARY)
            LoggerFactory.get_logger().debug(f'Sent P2P_FAILED for UID {uid.hex()}')
        except Exception as e:
            LoggerFactory.get_logger().error(f'Failed to send P2P_FAILED: {e}')

    def on_error(self, ws, error):
        LoggerFactory.get_logger().error(f'error:  {error} ')

    def on_close(self, ws, a, b):
        with OPEN_CLOSE_LOCK:
            LoggerFactory.get_logger().info(f'close, {a}, {b} ')
            self.heart_beat_task.is_running = False
            self.forward_client.close()
            # Stop P2P service
            if self.p2p_hole_punch:
                self.p2p_hole_punch.stop()
                LoggerFactory.get_logger().info('P2P hole punching service stopped')

def signal_handler(sig, frame):
    print('You pressed Ctrl+C!')
    os._exit(0)


def run_client(ws: websocket.WebSocketApp):
    while True:
        try:
            ws.run_forever()  # Set dispatcher to automatic reconnection
        except WebSocketException as e:
            LoggerFactory.get_logger().warn('WebSocketException: {}'.format(e))
            time.sleep(5)
        except Exception as e:
            LoggerFactory.get_logger().error(traceback.format_exc())
            time.sleep(5)
            continue
        LoggerFactory.get_logger().info(f'try after 2 seconds')
        time.sleep(2)


# Main function, initialize UDP client
def main():
    print('github: ', SystemConstant.GITHUB)
    if _UVLOOP_ENABLED:
        print('uvloop enabled')

    # Check if xxhash is available
    from common.encrypt_utils import EncryptUtils
    if not EncryptUtils.is_xxhash_available():
        print('Warning: xxhash not installed, please run: pip install xxhash')
        sys.exit(1)

    # Fix Python 3.10+ event loop issue
    # Need to explicitly create event loop when using uvloop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    config_data = get_config()
    signal.signal(signal.SIGINT, signal_handler)
    websocket.setdefaulttimeout(3)
    server_config = config_data['server']
    if not server_config['password']:
        raise Exception('Password cannot be empty, password is required')
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
    assert isinstance(compress_support, bool)
    if compress_support and not has_snappy:
        raise Exception('snappy is not installed')
    LoggerFactory.get_logger().info(f'start open {url}')
    if compress_support:
        if '?' in url:  # Append compress_support parameter
            url += '&c=' + json.dumps(compress_support)
        else:
            url += '?c=' + json.dumps(compress_support)
    ws = websocket.WebSocketApp(url)
    forward_client = TcpForwardClient(ws, compress_support)
    udp_forward_client = UdpForwardClient(ws, compress_support)  # Create UDP forward client
    heart_beat_task = HeatBeatTask(ws, SystemConstant.HEART_BEAT_INTERVAL)
    WebsocketClient(ws, forward_client, udp_forward_client, heart_beat_task, config_data)  # Pass UDP forward client
    LoggerFactory.get_logger().info('start run_forever')
    Thread(target=run_client, args=(ws,)).start()  # Use tornado's ioloop for convenient timeout setting
    Thread(target=heart_beat_task.run).start()
    Thread(target=forward_client.start_forward).start()
    # UDP client automatically starts receive thread on instantiation, no need to start separately
    ioloop.IOLoop.current().start()


if __name__ == '__main__':
    main()
