"""
多连接会话管理器

支持单个客户端建立多个 WebSocket 连接：
- 1 个控制连接 (control_handler): 用于心跳、配置、控制消息
- N 个数据连接 (data_handlers): 用于数据传输，不同 UID 分配到不同连接

向后兼容：
- 旧客户端（单连接）：只有 control_handler，数据也走 control_handler
- 新客户端（多连接）：control_handler + data_handlers
"""

import os
import time
import threading
from typing import Dict, List, Optional, TYPE_CHECKING

from common.logger_factory import LoggerFactory

if TYPE_CHECKING:
    from tornado.websocket import WebSocketHandler


class ClientSession:
    """客户端会话"""

    def __init__(self, client_name, session_token, control_handler,
                 is_multi_connection=False, compress_support=False, num_data_channels=4):
        self.client_name = client_name
        self.session_token = session_token
        self.control_handler = control_handler
        self.data_handlers = []  # type: List[WebSocketHandler]
        self.is_multi_connection = is_multi_connection
        self.created_at = time.time()
        self.compress_support = compress_support
        self.num_data_channels = num_data_channels

    def get_data_handler(self, uid):
        # type: (bytes) -> WebSocketHandler
        """
        根据 UID 获取应该使用的数据连接

        :param uid: 连接的 UID
        :return: WebSocket handler
        """
        if not self.is_multi_connection or not self.data_handlers:
            # 单连接模式或数据连接未就绪，使用控制连接
            return self.control_handler

        # 多连接模式：根据 UID hash 选择数据连接
        # 过滤掉 None 的连接
        valid_handlers = [h for h in self.data_handlers if h is not None]
        if not valid_handlers:
            return self.control_handler

        channel_index = int.from_bytes(uid[:2], 'big') % len(valid_handlers)
        return valid_handlers[channel_index]

    def add_data_handler(self, handler, channel_index):
        # type: (WebSocketHandler, int) -> bool
        """
        添加数据连接

        :param handler: WebSocket handler
        :param channel_index: 通道索引
        :return: 是否成功
        """
        # 确保列表足够大
        while len(self.data_handlers) <= channel_index:
            self.data_handlers.append(None)

        if self.data_handlers[channel_index] is not None:
            LoggerFactory.get_logger().warning(
                'Data channel {} already exists for {}'.format(channel_index, self.client_name)
            )
            return False

        self.data_handlers[channel_index] = handler
        LoggerFactory.get_logger().info(
            'Data channel {} added for {}, total: {}/{}'.format(
                channel_index, self.client_name,
                len([h for h in self.data_handlers if h is not None]),
                self.num_data_channels
            )
        )
        return True

    def remove_data_handler(self, handler):
        # type: (WebSocketHandler) -> None
        """移除数据连接"""
        for i, h in enumerate(self.data_handlers):
            if h == handler:
                self.data_handlers[i] = None
                LoggerFactory.get_logger().info(
                    'Data channel {} removed for {}'.format(i, self.client_name)
                )
                break

    def is_data_handler(self, handler):
        # type: (WebSocketHandler) -> bool
        """检查是否是数据连接"""
        return handler in self.data_handlers

    def get_all_handlers(self):
        # type: () -> List[WebSocketHandler]
        """获取所有连接（控制 + 数据）"""
        handlers = [self.control_handler] if self.control_handler else []
        handlers.extend([h for h in self.data_handlers if h is not None])
        return handlers


class SessionManager:
    """会话管理器（单例）"""

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        # client_name -> Session
        self.sessions = {}  # type: Dict[str, ClientSession]
        # session_token -> client_name (用于快速查找)
        self.token_to_client = {}  # type: Dict[bytes, str]
        # handler -> client_name (用于反向查找)
        self.handler_to_client = {}  # type: Dict[WebSocketHandler, str]
        self.lock = threading.Lock()

    @classmethod
    def get_instance(cls):
        # type: () -> SessionManager
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def create_session(self, client_name, control_handler,
                       is_multi_connection=False, compress_support=False,
                       num_data_channels=4):
        # type: (str, WebSocketHandler, bool, bool, int) -> Optional[ClientSession]
        """
        创建新会话

        :param client_name: 客户端名称
        :param control_handler: 控制连接
        :param is_multi_connection: 是否多连接模式
        :param compress_support: 是否支持压缩
        :param num_data_channels: 数据通道数量
        :return: Session 或 None（如果名称重复）
        """
        with self.lock:
            if client_name in self.sessions:
                return None  # 名称重复

            # 生成随机 session_token
            session_token = os.urandom(32)

            session = ClientSession(
                client_name=client_name,
                session_token=session_token,
                control_handler=control_handler,
                is_multi_connection=is_multi_connection,
                compress_support=compress_support,
                num_data_channels=num_data_channels
            )

            self.sessions[client_name] = session
            self.token_to_client[session_token] = client_name
            self.handler_to_client[control_handler] = client_name

            LoggerFactory.get_logger().info(
                'Session created for {}, multi_connection={}, token={}...'.format(
                    client_name, is_multi_connection, session_token.hex()[:16]
                )
            )

            return session

    def join_session(self, session_token, data_handler, channel_index):
        # type: (bytes, WebSocketHandler, int) -> Optional[ClientSession]
        """
        数据连接加入会话

        :param session_token: 会话令牌
        :param data_handler: 数据连接
        :param channel_index: 通道索引
        :return: Session 或 None（如果令牌无效）
        """
        with self.lock:
            client_name = self.token_to_client.get(session_token)
            if not client_name:
                LoggerFactory.get_logger().warning(
                    'Invalid session token: {}...'.format(session_token.hex()[:16])
                )
                return None

            session = self.sessions.get(client_name)
            if not session:
                return None

            # 检查控制连接是否还活着
            if not session.control_handler:
                LoggerFactory.get_logger().warning(
                    'Control connection dead for {}'.format(client_name)
                )
                return None

            if session.add_data_handler(data_handler, channel_index):
                self.handler_to_client[data_handler] = client_name
                return session

            return None

    def get_session_by_name(self, client_name):
        # type: (str) -> Optional[ClientSession]
        """通过客户端名称获取会话"""
        with self.lock:
            return self.sessions.get(client_name)

    def get_session_by_handler(self, handler):
        # type: (WebSocketHandler) -> Optional[ClientSession]
        """通过 handler 获取会话"""
        with self.lock:
            client_name = self.handler_to_client.get(handler)
            if client_name:
                return self.sessions.get(client_name)
            return None

    def get_session_by_token(self, token):
        # type: (bytes) -> Optional[ClientSession]
        """通过 token 获取会话"""
        with self.lock:
            client_name = self.token_to_client.get(token)
            if client_name:
                return self.sessions.get(client_name)
            return None

    def remove_handler(self, handler):
        # type: (WebSocketHandler) -> Optional[str]
        """
        移除连接

        :param handler: WebSocket handler
        :return: 如果是控制连接被移除，返回 client_name；否则返回 None
        """
        with self.lock:
            client_name = self.handler_to_client.pop(handler, None)
            if not client_name:
                return None

            session = self.sessions.get(client_name)
            if not session:
                return None

            if session.control_handler == handler:
                # 控制连接断开，整个会话失效
                LoggerFactory.get_logger().info(
                    'Control connection closed for {}, removing session'.format(client_name)
                )
                self._remove_session_internal(client_name)
                return client_name
            else:
                # 数据连接断开
                session.remove_data_handler(handler)
                return None

    def _remove_session_internal(self, client_name):
        # type: (str) -> None
        """内部方法：移除会话（需要已持有锁）"""
        session = self.sessions.pop(client_name, None)
        if session:
            self.token_to_client.pop(session.session_token, None)
            # 移除所有 handler 映射
            if session.control_handler:
                self.handler_to_client.pop(session.control_handler, None)
            for h in session.data_handlers:
                if h:
                    self.handler_to_client.pop(h, None)

    def remove_session(self, client_name):
        # type: (str) -> None
        """移除整个会话"""
        with self.lock:
            self._remove_session_internal(client_name)

    def client_name_exists(self, client_name):
        # type: (str) -> bool
        """检查客户端名称是否已存在"""
        with self.lock:
            return client_name in self.sessions
