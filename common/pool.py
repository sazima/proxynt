import time
from selectors import DefaultSelector, EVENT_READ

import select
import socket
import traceback
from typing import Dict, List

from common.logger_factory import LoggerFactory
from constant.system_constant import SystemConstant

"""
这里只监听了 socket  的可读状态 
"""


class SocketLoop:
    def __init__(self):
        self.is_running = True
        self.fileno_to_client: Dict[int, socket.socket] = dict()
        # self.call_back_function: List[callable] = []

    # def register(self, s: socket.socket):
    #     self.fileno_to_client[s.fileno()] = s
    #
    # def unregister(self, s: socket.socket):
    #     if s.fileno() in self.fileno_to_client:
    #         self.fileno_to_client.pop(s.fileno())
    #
    def add_callback_function(self, f: callable):
        """回调函数, 参数是 socket"""
        self.call_back_function.append(f)

    # def run(self):
    #     raise NotImplemented()

    def stop(self):
        self.is_running = False


class SelectPool(SocketLoop):

    def __init__(self):
        super(SelectPool, self).__init__()
        self.selector = DefaultSelector()

    def register(self, s: socket.socket, callable_):
        self.fileno_to_client[s.fileno()] = s
        self.selector.register(s, EVENT_READ, callable_)

    def unregister(self, s: socket.socket):
        if s.fileno() in self.fileno_to_client:
            self.fileno_to_client.pop(s.fileno())
        try:
            self.selector.unregister(s)
        except OSError:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def run(self):
        while self.is_running:
            # if not self.fileno_to_client:
            #     continue
            try:
                try:
                    ready = self.selector.select(timeout=SystemConstant.DEFAULT_TIMEOUT)
                except OSError:
                    # 监听列表为空的时候, windows会有os error
                    # LoggerFactory.get_logger().warn
                    time.sleep(0.5)
                    continue
                for key, mask in ready:
                    fileno = key.fd
                    client = self.fileno_to_client.get(fileno)
                    if client is None:
                        LoggerFactory.get_logger().warn(f'key error, {fileno}, self.fileno_to_client: {self.fileno_to_client}')
                        continue
                    callback = key.data
                    callback(client)
                    # self.call_back_function[0](client)
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
                time.sleep(1)
