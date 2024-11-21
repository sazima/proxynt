import logging
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from selectors import DefaultSelector, EVENT_READ

import socket
import traceback
from typing import Dict, List, Set

from common.logger_factory import LoggerFactory
from common.register_append_data import ResisterAppendData
from constant.system_constant import SystemConstant

"""
这里只监听了 socket  的可读状态 
"""
max_workers = 99


class SelectPool:

    def __init__(self):
        self.is_running = True
        self.fileno_to_client: Dict[int, socket.socket] = dict()
        self.selector = DefaultSelector()
        self.waiting_register_socket: Set = set()

        self.socket_to_lock: Dict[socket.socket, threading.Lock] = dict()
        self.executor = ThreadPoolExecutor(max_workers=max_workers)  #

    def stop(self):
        self.is_running = False

    def clear(self):
        self.fileno_to_client.clear()
        self.waiting_register_socket.clear()
        self.socket_to_lock.clear()

    def register(self, s: socket.socket, data: ResisterAppendData):
        self.socket_to_lock[s] = threading.Lock()
        self.fileno_to_client[s.fileno()] = s
        self.selector.register(s, EVENT_READ, data)

    def unregister_and_register_delay(self, s: socket.socket, data: ResisterAppendData, delay_time: int):
        """取消注册, 并在指定秒后注册"""

        def _register_again():
            try:
                if s not in self.socket_to_lock:
                    return
                is_exceed, remain = data.speed_limiter.is_exceed()
                if is_exceed:
                    # 再次延迟检测
                    if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
                        LoggerFactory.get_logger().debug('delay register again, maybe next: %.2f seconds "  ' % (remain / data.speed_limiter.max_speed))
                    threading.Timer(delay_time, _register_again).start()
                    return
                with self.socket_to_lock[s]:
                    if s in self.waiting_register_socket:  # 在等待列表中
                        self.waiting_register_socket.remove(s)
                        self.register(s, data)
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
                raise

        if s in self.waiting_register_socket:  # 不在等待列表中
            return
        if s not in self.socket_to_lock:
            return
        with self.socket_to_lock[s]:
            try:
                self.selector.unregister(s)
                self.waiting_register_socket.add(s)
            except OSError:
                LoggerFactory.get_logger().error(traceback.format_exc())
            threading.Timer(delay_time, _register_again).start()

    def unregister(self, s: socket.socket):
        if s not in self.socket_to_lock:
            LoggerFactory.get_logger().info('not register socket, skip')
            return
        with self.socket_to_lock[s]:
            if s in self.waiting_register_socket:
                self.waiting_register_socket.remove(s)
            if s.fileno() in self.fileno_to_client:
                self.fileno_to_client.pop(s.fileno())
            try:
                self.selector.unregister(s)
            except KeyError:
                # KeyError 代表已经注销
                pass
            except ValueError:
                # ? value error 代表已经注销?
                pass
            except OSError:
                LoggerFactory.get_logger().error(traceback.format_exc())
            self.socket_to_lock.pop(s)

    def run(self):
        while True:
            if not self.is_running:
                time.sleep(1)
                continue
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
                    data: ResisterAppendData = key.data
                    self.selector.unregister(client)  # register 防止一直就绪状态 耗cpu
                    if not self.socket_to_lock[client].acquire(blocking=False):
                        continue  # 已被其他线程处理，跳过
                    self.executor.submit(self._handle_client, client, data)
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
                time.sleep(1)

    def _handle_client(self, client, data):
        try:
            data.callable_(client, data)
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
        finally:
            self.socket_to_lock[client].release()
            self.selector.register(client, EVENT_READ, data)
