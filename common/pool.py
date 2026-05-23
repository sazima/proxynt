import logging
import socket
import threading
import time
import traceback
from selectors import DefaultSelector, EVENT_READ

from common.logger_factory import LoggerFactory
from common.register_append_data import ResisterAppendData
from constant.system_constant import SystemConstant

"""
单线程 selector 事件循环, 使用 modify 切换事件掩码
参考 shadowsocks eventloop 模式
"""


class SelectPool:

    def __init__(self):
        self.is_running = True
        self.fileno_to_client = dict()
        self.selector = DefaultSelector()

    def stop(self):
        self.is_running = False

    def clear(self):
        self.fileno_to_client.clear()

    def register(self, s: socket.socket, data: ResisterAppendData):
        self.fileno_to_client[s.fileno()] = s
        self.selector.register(s, EVENT_READ, data)

    def unregister(self, s: socket.socket):
        fileno = -1
        try:
            fileno = s.fileno()
        except Exception:
            pass
        if fileno in self.fileno_to_client:
            self.fileno_to_client.pop(fileno)
        try:
            self.selector.unregister(s)
        except (KeyError, ValueError):
            pass
        except OSError:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def pause_reading(self, s: socket.socket):
        """暂停监听可读事件 (modify 掩码为 0)"""
        try:
            key = self.selector.get_key(s)
            self.selector.modify(s, 0, key.data)
        except (KeyError, ValueError):
            pass

    def resume_reading(self, s: socket.socket):
        """恢复监听可读事件"""
        try:
            key = self.selector.get_key(s)
            self.selector.modify(s, EVENT_READ, key.data)
        except (KeyError, ValueError):
            pass

    def pause_and_resume_later(self, s: socket.socket, delay_time: float):
        """暂停可读监听, delay_time 秒后恢复"""
        self.pause_reading(s)
        threading.Timer(delay_time, self.resume_reading, args=(s,)).start()

    def run(self):
        while True:
            if not self.is_running:
                time.sleep(1)
                continue
            try:
                try:
                    ready = self.selector.select(timeout=SystemConstant.DEFAULT_TIMEOUT)
                except OSError:
                    time.sleep(0.5)
                    continue
                for key, mask in ready:
                    fileno = key.fd
                    client = self.fileno_to_client.get(fileno)
                    if client is None:
                        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                            LoggerFactory.get_logger().debug('fd %s not in fileno_to_client' % fileno)
                        continue
                    data: ResisterAppendData = key.data
                    try:
                        data.callable_(client, data)
                    except Exception:
                        LoggerFactory.get_logger().error(traceback.format_exc())
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
                time.sleep(1)
