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

# Try to enable uvloop for performance boost
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.speed_limit import SpeedLimiter
from common.websocket import WebSocketException, ABNF
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
SERVER_P2P_SIGNAL_PORT = 19999  # 服务端监听的 UDP 信令端口


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

        # P2P support
        self.public_ip: str = None
        self.public_port: int = None

        # Initialize P2P module
        self.p2p_hole_punch = P2PHolePunch()
        # 将 P2P 模块引用注入到 TcpForwardClient，实现数据发送时的路由选择
        self.forward_client.p2p_client = self.p2p_hole_punch
        # 将 WS 客户端引用注入到 P2PHolePunch，实现断线重连信令发送
        self.p2p_hole_punch.set_ws_client(self)
        self.forward_client.p2p_client = self.p2p_hole_punch

        # Callback for receiving P2P data (Tunnel -> Local)
        self.p2p_hole_punch.on_data_received = self._on_p2p_data_received

        self.p2p_hole_punch.start()

        # P2P pending offers (waiting for PEER_INFO)
        # uid_hex -> offer_data
        self.pending_p2p_offers: Dict[str, dict] = {}

        # Track if we've sent EXCHANGE (only send once per session)
        self.exchange_sent = False
        self.exchange_lock = threading.Lock()

        # 启动 UDP 地址刷新线程
        self._udp_refresh_running = True
        self._start_udp_refresh()

    def _start_udp_refresh(self):
        """
        定期向服务器 UDP 端口发送心跳，确保 NAT 映射不过期，
        并让服务器获知准确的 UDP 公网端口。
        """
        parsed_url = urlparse(self.ws.url)
        server_host = parsed_url.hostname
        # server_host = self.config_data['server']['host']
        # 优先使用配置的端口，或者默认端口 19999
        server_port = SERVER_P2P_SIGNAL_PORT
        client_name = self.config_data['client_name']

        def refresh_loop():
            # 这里创建一个持久的 socket 用于保活
            # 注意：理想情况下 P2P 打洞应复用此 Socket，但目前架构下暂独立
            # 这里的目的是让服务器知道 "Client Name -> Public IP:Port" 的映射
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

            LoggerFactory.get_logger().info(f"Starting UDP address refresh to {server_host}:{server_port}")

            while self._udp_refresh_running:
                try:
                    # 协议格式：P2P_PING:客户端名
                    msg = f'P2P_PING:{client_name}'.encode('utf-8')
                    sock.sendto(msg, (server_host, server_port))
                except Exception as e:
                    # LoggerFactory.get_logger().debug(f"UDP refresh failed: {e}")
                    pass
                time.sleep(15) # 15秒一次心跳

            try:
                sock.close()
            except:
                pass

        t = threading.Thread(target=refresh_loop, daemon=True)
        t.start()

    def send_p2p_pre_connect(self, target_client_name):
        """发送 P2P 预连接请求"""
        if not target_client_name: return
        try:
            # 需要在 MessageTypeConstant 中定义 P2P_PRE_CONNECT = 'p2p_pre_connect'
            msg_type = getattr(MessageTypeConstant, 'P2P_PRE_CONNECT', 'p2p_pre_connect')

            msg: MessageEntity = {
                'type_': msg_type,
                'data': {
                    'target_client': target_client_name
                }
            }
            if self.ws.sock and self.ws.sock.connected:
                self.ws.send(NatSerialization.dumps(msg, ContextUtils.get_password(), self.compress_support), websocket.ABNF.OPCODE_BINARY)
                LoggerFactory.get_logger().info(f"Sent P2P pre-connect request for target: {target_client_name}")
        except Exception as e:
            LoggerFactory.get_logger().error(f"Failed to send pre-connect: {e}")

    def on_message(self, ws, message: bytes):
        try:
            message_data: MessageEntity = NatSerialization.loads(message, ContextUtils.get_password(), self.compress_support)
            self.heart_beat_task.set_recv_heart_beat_time(time.time())

            msg_type = message_data['type_']

            # Handle TCP messages
            if msg_type == MessageTypeConstant.WEBSOCKET_OVER_TCP:
                data: TcpOverWebsocketMessage = message_data['data']
                uid = data['uid']
                name = data['name']
                b = data['data']

                # 注册 Session 关联 (P2P 模块需要知道这个 UID 属于哪个服务/对端)
                # 注意：这里我们假设 socket name 包含了对端信息，或者通过其他方式关联
                # 在 Multiplexing 模式下，接收端不需要在这里 initiate_connection，
                # 因为隧道建立是独立的。这里只需要处理数据。

                create_result = self.forward_client.create_socket(name, uid, data['ip_port'], name_to_speed_limiter.get(name))
                if create_result:
                    self.forward_client.send_by_uid(uid, b)

            # Handle TCP connection request
            elif msg_type == MessageTypeConstant.REQUEST_TO_CONNECT:
                data: TcpOverWebsocketMessage = message_data['data']
                uid = data['uid']
                name = data['name']

                # 注册 P2P Session (虽然是被动连接，但如果 P2P 通了，回包也要走 P2P)
                # 这里暂时无法直接得知 Peer Name，除非协议里带了。
                # 目前逻辑是：发送端决定走 P2P，接收端收到 P2P 数据包后自动响应。

                self.forward_client.create_socket(name, uid, data['ip_port'], name_to_speed_limiter.get(name))

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
                self.udp_forward_client.create_udp_socket(name, uid, data['ip_port'], name_to_speed_limiter.get(name))

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

                    # 【核心】启动后延迟触发 P2P 预连接
                    # 延迟是为了确保 UDP Refresh 包已经到达服务器，服务器有了最新的 UDP 端口
                    threading.Timer(3.0, self._trigger_initial_p2p, args=(c2c_rules,)).start()

            # Handle P2P OFFER
            elif msg_type == MessageTypeConstant.P2P_OFFER:
                self._handle_p2p_offer(message_data['data'])

            # Handle P2P PEER_INFO (contains peer's actual UDP port from EXCHANGE)
            elif msg_type == MessageTypeConstant.P2P_PEER_INFO:
                self._handle_p2p_peer_info(message_data['data'])

            # Handle P2P Handshake Signals (Success/Failed) from Peer
            # 在 Multiplexing 模式下，握手在 p2p_hole_punch 内部闭环，这里主要是打日志
            elif msg_type == MessageTypeConstant.P2P_SUCCESS:
                LoggerFactory.get_logger().info(f"P2P Success signal received for {message_data.get('data', {}).get('uid')}")
            elif msg_type == MessageTypeConstant.P2P_FAILED:
                LoggerFactory.get_logger().warn(f"P2P Failed signal received for {message_data.get('data', {}).get('uid')}")

        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def _trigger_initial_p2p(self, rules):
        """遍历规则，对启用了 P2P 的目标发起预连接"""
        target_clients = set()
        for rule in rules:
            # 只处理自己作为源端的规则
            if rule.get('p2p_enabled', True) and rule.get('target_client'):
                target_clients.add(rule['target_client'])

        for target in target_clients:
            self.send_p2p_pre_connect(target)

    def _handle_p2p_offer(self, data: dict):
        """Handle P2P OFFER message from server"""
        if not self.p2p_hole_punch:
            return

        LoggerFactory.get_logger().info(f'[DEBUG] Received P2P_OFFER: {data}')

        # Hex to bytes
        uid_hex = data['uid']
        try:
            uid = bytes.fromhex(uid_hex)
        except:
            uid = uid_hex.encode() # Fallback

        role = data['role']
        peer_client = data['peer_client']

        if self.p2p_hole_punch.is_tunnel_active(peer_client):
            LoggerFactory.get_logger().info(f"⚡ Tunnel to {peer_client} exists. Multiplexing UID {uid_hex} over existing tunnel.")
            # 仅仅注册 UID 映射关系即可
            self.p2p_hole_punch.register_session(uid, peer_client)
            if role == 'responder':
                service_name = data.get('service_name', 'unknown')
                ip_port = data.get('ip_port') # 例如 "127.0.0.1:22"

                if ip_port:
                    LoggerFactory.get_logger().info(f"Multiplexing: Connecting to local target {ip_port} for UID {uid_hex}")
                    # 主动建立到本地服务的 TCP 连接
                    self.forward_client.create_socket(
                        service_name,
                        uid,
                        ip_port,
                        name_to_speed_limiter.get(service_name)
                    )

            return # 复用流程结束

        # Check if this is new EXCHANGE-based flow
        need_exchange = data.get('need_exchange', False)

        LoggerFactory.get_logger().info(f'[DEBUG] need_exchange={need_exchange}, has peer_public_ip={("peer_public_ip" in data)}')

        if need_exchange:
            # New flow: Send EXCHANGE first, then wait for PEER_INFO
            LoggerFactory.get_logger().info(f'P2P Offer received (EXCHANGE mode): Peer={peer_client}, Role={role}')

            # Save this offer for later processing (when we receive PEER_INFO)
            self.pending_p2p_offers[uid_hex] = data

            # Send EXCHANGE packet to server to establish UDP NAT mapping
            # CRITICAL: Must send from actual punching socket, not temp socket!
            self._send_exchange_packet(peer_client)
        else:
            # Legacy flow: Direct punching (for backward compatibility)
            peer_public_ip = data.get('peer_public_ip')
            try:
                peer_public_port = int(data['peer_public_port'])
            except (ValueError, TypeError):
                LoggerFactory.get_logger().error(f'Invalid peer port: {data.get("peer_public_port")}')
                return

            LoggerFactory.get_logger().info(f'Processing P2P Offer (legacy): Peer={peer_client}, Addr={peer_public_ip}:{peer_public_port}')

            # Initiate P2P connection (Multiplexing mode)
            self.p2p_hole_punch.initiate_connection(uid, peer_client, peer_public_ip, peer_public_port)

    def _send_exchange_packet(self, peer_name: str):
        """
        Send EXCHANGE packet to server to establish UDP NAT mapping.
        """
        try:
            # Get server host from config
            server_url = self.config_data['server']['url']
            parsed = urlparse(server_url)
            server_host = parsed.hostname

            # Create EXCHANGE packet: b'EXCHANGE:' + client_name
            client_name = self.config_data['client_name']
            exchange_packet = b'EXCHANGE:' + client_name.encode('utf-8')

            # Send to server's P2P exchange port (default 19999)
            exchange_port = self.config_data.get('p2p_exchange_port', SERVER_P2P_SIGNAL_PORT)

            # Prepare punching sockets and get first socket for EXCHANGE
            exchange_sock = self.p2p_hole_punch.prepare_for_exchange(peer_name)

            # [修改点 1] 增加对 Socket 有效性的检查 (fileno != -1)
            if not exchange_sock or exchange_sock.fileno() == -1:
                LoggerFactory.get_logger().warn('Socket not ready or closed, skipping EXCHANGE')
                return

            # Send 3 times to avoid packet loss
            for _ in range(3):
                try:
                    # [修改点 2] 发送前再次检查，防止发送过程中被其他线程关闭
                    if exchange_sock.fileno() == -1:
                        return
                    exchange_sock.sendto(exchange_packet, (server_host, exchange_port))
                    time.sleep(0.05)
                except OSError as e:
                    # [修改点 3] 忽略 "Bad file descriptor" 错误，这表示 Socket 被轮换了
                    if e.errno == 9:
                        return
                    # 其他 IO 错误通过日志记录但不崩溃
                    LoggerFactory.get_logger().debug(f"Send EXCHANGE partial fail: {e}")

            # Get local port for logging
            try:
                local_port = exchange_sock.getsockname()[1]
                LoggerFactory.get_logger().info(f'Sent EXCHANGE from local port {local_port} to {server_host}:{exchange_port}')
            except Exception:
                # 获取端口名失败通常意味着 Socket 已关闭，忽略即可
                pass

        except Exception as e:
            # 捕获所有其他异常，防止线程崩溃
            LoggerFactory.get_logger().error(f'Failed to send EXCHANGE packet: {e}')
            # LoggerFactory.get_logger().error(traceback.format_exc())

    def _handle_p2p_peer_info(self, data: dict):
        """Handle P2P_PEER_INFO from server (contains peer's actual UDP port)"""
        uid_hex = data['uid']
        peer_client = data['peer_client']
        peer_ip = data['peer_ip']
        peer_udp_port = int(data['peer_udp_port'])

        LoggerFactory.get_logger().info(
            f'Received P2P_PEER_INFO: Peer={peer_client}, UDP={peer_ip}:{peer_udp_port}, UID={uid_hex}'
        )

        # 尝试移除 pending，无论是否存在都继续往下走
        self.pending_p2p_offers.pop(uid_hex, None)

        # Convert uid
        try:
            uid = bytes.fromhex(uid_hex)
        except:
            uid = uid_hex.encode()

        # 无论是否是 Multiplexing，都将最新的 Peer 信息传递给底层
        # 底层 P2PHolePunch.initiate_connection 负责判断：
        # - 如果地址没变 -> 忽略
        # - 如果地址变了 -> 触发 Re-punching (复用 Socket)
        self.p2p_hole_punch.initiate_connection(uid, peer_client, peer_ip, peer_udp_port)

    def _on_p2p_data_received(self, uid: bytes, data: bytes):
        """
        Callback when data comes from P2P Tunnel.
        Forward it to the local socket (via tcp_forward_client).
        """
        # 直接调用 send_by_uid，它会找到对应的 socket 并发送数据给本地应用 (如 SSH Server)
        self.forward_client.send_by_uid(uid, data)

    def _send_p2p_failed(self, uid: bytes):
        """Notify server about failure (Legacy support)"""
        try:
            message: MessageEntity = {
                'type_': MessageTypeConstant.P2P_FAILED,
                'data': {'uid': uid.hex() if isinstance(uid, bytes) else uid}
            }
            self.ws.send(NatSerialization.dumps(message, ContextUtils.get_password(), self.compress_support), websocket.ABNF.OPCODE_BINARY)
        except Exception:
            pass

    def on_open(self, ws):
        with OPEN_CLOSE_LOCK:
            try:
                LoggerFactory.get_logger().info('WS Open: Resetting clients...')
                self.heart_beat_task.is_running = False
                self.forward_client.close()
                self.udp_forward_client.close()

                # Stop P2P if running
                if self.p2p_hole_punch:
                    self.p2p_hole_punch.stop()
                    # Re-start P2P service
                    self.p2p_hole_punch.start()

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

                ws.send(NatSerialization.dumps(message, ContextUtils.get_password(), self.compress_support), websocket.ABNF.OPCODE_BINARY)
                self.forward_client.set_running(True)
                self.udp_forward_client.set_running(True)
                self.heart_beat_task.is_running = True

                # Restart UDP refresh loop
                if not self._udp_refresh_running:
                    self._udp_refresh_running = True
                    self._start_udp_refresh()

            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())

    def on_error(self, ws, error):
        LoggerFactory.get_logger().error(f'WS Error: {error}')

    def on_close(self, ws, a, b):
        with OPEN_CLOSE_LOCK:
            LoggerFactory.get_logger().info(f'WS Closed: {a}, {b}')
            self.heart_beat_task.is_running = False
            self.forward_client.close()
            # Stop P2P
            if self.p2p_hole_punch:
                self.p2p_hole_punch.stop()
            # Stop UDP refresh
            self._udp_refresh_running = False


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
        print('uvloop enabled')

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

    LoggerFactory.get_logger().info(f'Connecting to {url}')

    if compress_support:
        sep = '&' if '?' in url else '?'
        url += sep + 'c=' + json.dumps(compress_support)

    ws = websocket.WebSocketApp(url)

    # Init Clients
    forward_client = TcpForwardClient(ws, compress_support)
    udp_forward_client = UdpForwardClient(ws, compress_support)
    heart_beat_task = HeatBeatTask(ws, SystemConstant.HEART_BEAT_INTERVAL)

    # Init Controller
    WebsocketClient(ws, forward_client, udp_forward_client, heart_beat_task, config_data)

    LoggerFactory.get_logger().info('Client started')

    Thread(target=run_client, args=(ws,)).start()
    Thread(target=heart_beat_task.run).start()
    Thread(target=forward_client.start_forward).start()

    ioloop.IOLoop.current().start()


if __name__ == '__main__':
    main()