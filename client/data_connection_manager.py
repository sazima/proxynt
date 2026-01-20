"""
客户端数据连接管理器

管理多个 WebSocket 数据连接：
- 建立 N 个数据连接并加入 Session
- 根据 UID hash 选择数据连接发送数据
- 处理数据连接断开重连
"""

import json
import threading
import time
import traceback
from typing import List, Optional, Callable
from urllib.parse import urlparse

from common import websocket
from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class DataConnection:
    """单个数据连接"""

    def __init__(self, channel_index: int, url: str, session_token: str,
                 compress_support: bool, on_message_callback: Callable):
        self.channel_index = channel_index
        self.url = url
        self.session_token = session_token
        self.compress_support = compress_support
        self.on_message_callback = on_message_callback
        self.ws: Optional[websocket.WebSocketApp] = None
        self.is_connected = False
        self.is_joined = False
        self.running = True
        self.thread: Optional[threading.Thread] = None

    def start(self):
        """启动数据连接"""
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        """连接循环"""
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error
                )
                self.ws.run_forever()
            except Exception as e:
                LoggerFactory.get_logger().error(f'Data connection {self.channel_index} error: {e}')

            if self.running:
                LoggerFactory.get_logger().info(f'Data connection {self.channel_index} reconnecting in 2s...')
                time.sleep(2)

    def _on_open(self, ws):
        """连接建立后发送 JOIN_SESSION"""
        LoggerFactory.get_logger().info(f'Data connection {self.channel_index} opened, sending JOIN_SESSION')
        self.is_connected = True

        # 发送 JOIN_SESSION 消息
        join_message: MessageEntity = {
            'type_': MessageTypeConstant.JOIN_SESSION,
            'data': {
                'session_token': self.session_token,
                'channel_index': self.channel_index
            }
        }
        try:
            ws.send(
                NatSerialization.dumps(join_message, ContextUtils.get_password(), self.compress_support),
                websocket.ABNF.OPCODE_BINARY
            )
        except Exception as e:
            LoggerFactory.get_logger().error(f'Failed to send JOIN_SESSION: {e}')

    def _on_message(self, ws, message: bytes):
        """处理收到的消息"""
        try:
            message_data: MessageEntity = NatSerialization.loads(
                message, ContextUtils.get_password(), self.compress_support
            )

            # 处理 JOIN_SESSION 响应
            if message_data['type_'] == MessageTypeConstant.JOIN_SESSION:
                data = message_data['data']
                if data.get('success'):
                    self.is_joined = True
                    LoggerFactory.get_logger().info(
                        f'Data connection {self.channel_index} joined session successfully'
                    )
                else:
                    LoggerFactory.get_logger().error(
                        f'Data connection {self.channel_index} failed to join session'
                    )
                    ws.close()
                return

            # 其他消息转发给回调处理
            if self.on_message_callback:
                self.on_message_callback(message_data)

        except Exception as e:
            LoggerFactory.get_logger().error(f'Data connection {self.channel_index} message error: {e}')
            LoggerFactory.get_logger().error(traceback.format_exc())

    def _on_close(self, ws, code, reason):
        """连接关闭"""
        LoggerFactory.get_logger().info(
            f'Data connection {self.channel_index} closed: code={code}, reason={reason}'
        )
        self.is_connected = False
        self.is_joined = False

    def _on_error(self, ws, error):
        """连接错误"""
        LoggerFactory.get_logger().error(f'Data connection {self.channel_index} error: {error}')

    def send(self, data: bytes):
        """发送数据"""
        if self.ws and self.is_joined:
            try:
                self.ws.send(data, websocket.ABNF.OPCODE_BINARY)
                return True
            except Exception as e:
                LoggerFactory.get_logger().error(f'Data connection {self.channel_index} send error: {e}')
        return False

    def stop(self):
        """停止连接"""
        self.running = False
        self.is_connected = False
        self.is_joined = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass


class DataConnectionManager:
    """数据连接管理器"""

    def __init__(self, server_url: str, compress_support: bool, on_message_callback: Callable):
        self.server_url = server_url
        self.compress_support = compress_support
        self.on_message_callback = on_message_callback

        self.session_token: Optional[str] = None
        self.num_channels: int = 4
        self.data_connections: List[DataConnection] = []
        self.is_multi_connection: bool = False
        self.lock = threading.Lock()

    def setup_data_connections(self, session_token: str, num_channels: int = 4):
        """
        设置数据连接

        :param session_token: 会话令牌（hex 格式）
        :param num_channels: 数据通道数量
        """
        with self.lock:
            # 清理旧连接
            self.stop_all()

            self.session_token = session_token
            self.num_channels = num_channels
            self.is_multi_connection = True

            LoggerFactory.get_logger().info(
                f'Setting up {num_channels} data connections, token={session_token[:16]}...'
            )

            # 创建数据连接
            for i in range(num_channels):
                conn = DataConnection(
                    channel_index=i,
                    url=self.server_url,
                    session_token=session_token,
                    compress_support=self.compress_support,
                    on_message_callback=self.on_message_callback
                )
                self.data_connections.append(conn)
                conn.start()

            LoggerFactory.get_logger().info(f'Started {num_channels} data connections')

    def get_data_connection(self, uid: bytes) -> Optional[DataConnection]:
        """
        根据 UID 获取数据连接

        :param uid: 连接 UID
        :return: 数据连接，如果没有可用的返回 None
        """
        if not self.is_multi_connection or not self.data_connections:
            return None

        # 根据 UID hash 选择通道
        channel_index = int.from_bytes(uid[:2], 'big') % len(self.data_connections)
        conn = self.data_connections[channel_index]

        # 检查连接是否可用
        if conn.is_joined:
            return conn

        # 如果选中的通道不可用，尝试其他通道
        for conn in self.data_connections:
            if conn.is_joined:
                return conn

        return None

    def send_by_uid(self, uid: bytes, data: bytes) -> bool:
        """
        根据 UID 发送数据

        :param uid: 连接 UID
        :param data: 要发送的数据
        :return: 是否成功发送
        """
        conn = self.get_data_connection(uid)
        if conn:
            return conn.send(data)
        return False

    def is_ready(self) -> bool:
        """检查是否有数据连接可用"""
        if not self.is_multi_connection:
            return False
        for conn in self.data_connections:
            if conn.is_joined:
                return True
        return False

    def stop_all(self):
        """停止所有数据连接"""
        for conn in self.data_connections:
            conn.stop()
        self.data_connections.clear()
        self.is_multi_connection = False
        self.session_token = None
