import asyncio
import json
import logging
import os
import queue
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
from tornado import gen, ioloop
from tornado.websocket import websocket_connect

PROTOCOL_VERSION = 2

# Try to enable high-performance event loop (20-30% boost)
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
from client.udp_forward_client import UdpForwardClient
from client.heart_beat_task import HeatBeatTask
from client.tcp_forward_client import TcpForwardClient
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


class WsSender:
    """Thread-safe WebSocket sender wrapping Tornado WebSocketClientConnection."""

    MAX_PENDING_WRITES = 32

    def __init__(self):
        self._ws = None
        self._ioloop = None
        self.connected = False
        self.sock = self
        self._write_sem = threading.Semaphore(self.MAX_PENDING_WRITES)
        self._ioloop_thread_id = threading.main_thread().ident

    def set_connection(self, ws, ioloop_instance=None):
        self._ws = ws
        self._ioloop = ioloop_instance or ioloop.IOLoop.current()
        self.connected = ws is not None
        self._write_sem = threading.Semaphore(self.MAX_PENDING_WRITES)

    def send(self, data, opcode=None):
        ws = self._ws
        io = self._ioloop
        if ws is None or io is None:
            return

        if threading.current_thread().ident == self._ioloop_thread_id:
            io.add_callback(ws.write_message, data, binary=True)
            return

        acquired = self._write_sem.acquire(timeout=10)
        if not acquired or self._ws is None:
            return
        io.add_callback(self._do_write, data)

    def _do_write(self, data):
        ws = self._ws
        if ws is None:
            self._write_sem.release()
            return
        try:
            future = ws.write_message(data, binary=True)
            ioloop.IOLoop.current().add_future(future, lambda f: self._on_write_done())
        except Exception:
            self._write_sem.release()

    def _on_write_done(self):
        self._write_sem.release()

    def close(self):
        ws = self._ws
        io = self._ioloop
        if ws and io:
            io.add_callback(ws.close)


class MessageProcessor:
    """Processes incoming WebSocket messages in a dedicated background thread."""

    def __init__(self):
        self._queue = queue.Queue()
        self._handler = None
        self._thread = Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_handler(self, handler):
        self._handler = handler

    def submit(self, msg):
        self._queue.put(msg)

    def _run(self):
        while True:
            msg = self._queue.get()
            if msg is None:
                return
            handler = self._handler
            if handler is None:
                continue
            try:
                handler(None, msg)
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())

    def stop(self):
        self._queue.put(None)


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
    def __init__(self, ws_sender: WsSender, tcp_forward_client, udp_forward_client, heart_beat_task, config_data: ClientConfigEntity):
        self.ws_sender: WsSender = ws_sender
        self.forward_client: TcpForwardClient = tcp_forward_client
        self.udp_forward_client: UdpForwardClient = udp_forward_client
        self.heart_beat_task: HeatBeatTask = heart_beat_task
        self.config_data: ClientConfigEntity = config_data
        self.compress_support: bool = config_data['server']['compress']
        self.protocol_version: int = PROTOCOL_VERSION

        # P2P disabled
        self.public_ip: str = None
        self.public_port: int = None

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

                source_client = data.get('source_client')

                speed_limit = data.get('speed_limit', 0.0)
                if speed_limit > 0:
                    LoggerFactory.get_logger().info('Using speed limit %s for %s' % (speed_limit, name))
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

                c2c_rules = push_config.get('client_to_client_rules', [])
                if c2c_rules:
                    LoggerFactory.get_logger().info('Received %d C2C rules' % len(c2c_rules))
                    self.forward_client.setup_c2c_tcp_listeners(c2c_rules)
                    self.udp_forward_client.setup_c2c_udp_listeners(c2c_rules)

            # P2P punch disabled
            elif msg_type == MessageTypeConstant.P2P_PUNCH_REQUEST:
                LoggerFactory.get_logger().info('P2P punch disabled, ignoring request')
            elif msg_type == MessageTypeConstant.P2P_PEER_INFO:
                LoggerFactory.get_logger().info('P2P punch disabled, ignoring peer info')

        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def on_open(self):
        with OPEN_CLOSE_LOCK:
            try:
                LoggerFactory.get_logger().info('WS Open: Resetting clients...')
                self.heart_beat_task.is_running = False
                self.forward_client.close()
                self.udp_forward_client.close()


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
                    'p2p_supported': False
                }
                message: MessageEntity = {
                    'type_': MessageTypeConstant.PUSH_CONFIG,
                    'data': push_configs
                }
                self.heart_beat_task.set_recv_heart_beat_time(time.time())

                self.ws_sender.send(
                    NatSerialization.dumps(message, ContextUtils.get_password(), self.compress_support, self.protocol_version)
                )
                self.forward_client.set_running(True)
                self.udp_forward_client.set_running(True)
                self.heart_beat_task.is_running = True

            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())

    def on_close(self, ws=None, a=None, b=None):
        with OPEN_CLOSE_LOCK:
            LoggerFactory.get_logger().info('WS Closed: %s, %s' % (a, b))
            self.heart_beat_task.is_running = False
            self.forward_client.close()


async def connect_loop(url, client, processor):
    """
    Async reconnect loop using Tornado websocket_connect.
    Runs on the main IOLoop. Automatically reconnects on disconnect.

    on_message only does queue.put (instant), so the IOLoop is never blocked
    by downstream socket I/O (sendall / connect).
    """
    io = ioloop.IOLoop.current()

    while True:
        close_future = asyncio.Future()

        def on_message(msg):
            if msg is None:
                if not close_future.done():
                    close_future.set_result(None)
            else:
                processor.submit(msg)

        try:
            ws_conn = await websocket_connect(
                url,
                on_message_callback=on_message,
                connect_timeout=10,
            )
            client.ws_sender.set_connection(ws_conn, io)
            client.on_open()
            await close_future
            client.ws_sender.set_connection(None, None)
            client.on_close()
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
            client.ws_sender.set_connection(None, None)

        LoggerFactory.get_logger().info('Reconnecting in 2 seconds...')
        await gen.sleep(2)


def signal_handler(sig, frame):
    print('You pressed Ctrl+C!')
    os._exit(0)


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
        url += '%s:%s%s' % (server_config['host'], str(server_config['port']), server_config['path'])

    config_data['server'].setdefault('compress', False)
    compress_support = config_data['server']['compress']
    if compress_support and not has_snappy:
        raise Exception('snappy is not installed')

    config_data['server'].setdefault('protocol_version', 2)
    protocol_version = config_data['server']['protocol_version']

    LoggerFactory.get_logger().info('Connecting to %s' % url)

    if compress_support:
        sep = '&' if '?' in url else '?'
        url += sep + 'c=' + json.dumps(compress_support)

    sep = '&' if '?' in url else '?'
    url += sep + 'v=' + str(protocol_version)

    # Single shared sender — thread-safe wrapper around Tornado ws connection
    ws_sender = WsSender()

    # Init Clients
    forward_client = TcpForwardClient(ws_sender, compress_support, protocol_version)
    udp_forward_client = UdpForwardClient(ws_sender, compress_support, protocol_version)
    heart_beat_task = HeatBeatTask(ws_sender, SystemConstant.HEART_BEAT_INTERVAL, protocol_version)

    # Init Controller
    client = WebsocketClient(ws_sender, forward_client, udp_forward_client, heart_beat_task, config_data)

    LoggerFactory.get_logger().info('Client started')

    # SelectPool runs in its own thread (local socket I/O)
    Thread(target=forward_client.start_forward, daemon=True).start()

    io = ioloop.IOLoop.current()

    # Heartbeat via PeriodicCallback (no thread needed)
    heart_beat_task.start(io)

    # Message processor: decouples IOLoop from blocking socket ops
    processor = MessageProcessor()
    processor.set_handler(client.on_message)

    # WebSocket connect loop runs on IOLoop (no thread needed)
    io.spawn_callback(connect_loop, url, client, processor)

    io.start()


if __name__ == '__main__':
    main()
