import select
import socket
from typing import Dict, List

from constant.system_constant import SystemConstant

"""
这里只监听了 socket  的可读状态 
"""


class SocketLoop:
    def __init__(self):
        self.is_running = False
        self.fileno_to_client: Dict[int, socket.socket] = dict()
        self.call_back_function: List[callable] = []

    def register(self, s: socket.socket):
        self.fileno_to_client[s.fileno()] = s

    def unregister(self, s: socket.socket):
        self.fileno_to_client.pop(s.fileno())

    def add_callback_function(self, f: callable):
        """回调函数, 参数是 socket"""
        self.call_back_function.append(f)

    def run(self):
        raise NotImplemented()

    def stop(self):
        self.is_running = False


class SelectPool(SocketLoop):
    def run(self):
        while self.is_running:
            s_list = (self.fileno_to_client.values())
            try:
                rs, ws, es = select.select(s_list, [], [], SystemConstant.DEFAULT_TIMEOUT)
            except ValueError:
                continue
            for each in rs:
                for f in self.call_back_function:
                    f(each)


class EPool(SocketLoop):
    def __init__(self):
        super(EPool, self).__init__()
        self.poll = select.epoll()

    def run(self):
        while self.is_running:
            events = self.poll.poll(SystemConstant.DEFAULT_TIMEOUT)
            # 事件是一个`(fileno, 事件code)`的元组
            for fileno, event in events:
                client = self.fileno_to_client[fileno]
                self.call_back_function[0](client)

    def register(self, s: socket.socket):
        self.fileno_to_client[s.fileno()] = s
        self.poll.register(s.fileno(), select.EPOLLIN)

    def unregister(self, s: socket.socket):
        if s.fileno() in self.fileno_to_client:
            self.fileno_to_client.pop(s.fileno())
            self.poll.unregister(s.fileno())
