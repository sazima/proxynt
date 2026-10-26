import logging
import socket
import threading
import time
import traceback
from selectors import DefaultSelector, EVENT_READ
from typing import Dict, Set

from common.logger_factory import LoggerFactory
from common.register_append_data import ResisterAppendData
from constant.system_constant import SystemConstant

"""
单线程 selector 事件循环, 使用 modify 切换事件掩码
参考 shadowsocks eventloop 模式
"""

PAUSE_REASON_SPEED_LIMIT = 'speed_limit'
PAUSE_REASON_FLOW_CONTROL = 'flow_control'
PAUSE_REASON_EARLY_DATA = 'early_data'
PAUSE_REASON_WS_BACKPRESSURE = 'ws_backpressure'


class SelectPool:

    def __init__(self):
        self.is_running = True
        self.fileno_to_client = dict()
        self.selector = DefaultSelector()
        # 其他线程 register 时唤醒阻塞中的 select，否则 Windows 上新 socket 要等本轮 select 超时才被监听
        self._wakeup_r, self._wakeup_w = socket.socketpair()
        self._wakeup_r.setblocking(False)
        self._wakeup_w.setblocking(False)
        self.selector.register(self._wakeup_r, EVENT_READ, None)
        # 每个 socket 的暂停原因 (如 speed_limit / flow_control), 全部解除才恢复读取
        self._pause_reasons: Dict[socket.socket, Set[str]] = dict()
        self._paused_data: Dict[socket.socket, ResisterAppendData] = dict()  # 暂停期间 socket 不在 selector 中, 保存其 data
        self._pause_lock = threading.Lock()

    def _wakeup(self):
        try:
            self._wakeup_w.send(b'\x00')
        except OSError:
            pass

    def _drain_wakeup(self):
        try:
            while self._wakeup_r.recv(4096):
                pass
        except OSError:
            pass

    def stop(self):
        self.is_running = False

    def clear(self):
        self.fileno_to_client.clear()

    def register(self, s: socket.socket, data: ResisterAppendData):
        self.fileno_to_client[s.fileno()] = s
        self.selector.register(s, EVENT_READ, data)
        self._wakeup()

    def unregister(self, s: socket.socket):
        fileno = -1
        try:
            fileno = s.fileno()
        except Exception:
            pass
        if fileno in self.fileno_to_client:
            self.fileno_to_client.pop(fileno)
        with self._pause_lock:
            self._pause_reasons.pop(s, None)
            self._paused_data.pop(s, None)
        try:
            self.selector.unregister(s)
        except (KeyError, ValueError):
            pass
        except OSError:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def pause_reading(self, s: socket.socket, reason: str = PAUSE_REASON_SPEED_LIMIT):
        """
        暂停监听可读事件
        不能用 modify(s, 0): Python 3.6 的 modify 会先 unregister 再以 0 掩码 register 抛 ValueError, socket 被永久移除
        这里直接 unregister 并保存 data, 恢复时重新 register
        """
        with self._pause_lock:
            reasons = self._pause_reasons.get(s)
            if reasons:
                reasons.add(reason)
                return
            try:
                key = self.selector.get_key(s)
            except (KeyError, ValueError):
                return
            self._pause_reasons[s] = {reason}
            self._paused_data[s] = key.data
            try:
                self.selector.unregister(s)
            except (KeyError, ValueError):
                pass

    def resume_reading(self, s: socket.socket, reason: str = PAUSE_REASON_SPEED_LIMIT):
        """解除一个暂停原因, 没有其他原因时恢复监听可读事件"""
        with self._pause_lock:
            reasons = self._pause_reasons.get(s)
            if not reasons or reason not in reasons:
                return
            reasons.discard(reason)
            if reasons:
                return
            self._pause_reasons.pop(s, None)
            data = self._paused_data.pop(s, None)
            try:
                self.selector.register(s, EVENT_READ, data)
            except (KeyError, ValueError, OSError):
                return
        self._wakeup()

    def pause_and_resume_later(self, s: socket.socket, delay_time: float):
        """暂停可读监听, delay_time 秒后恢复"""
        self.pause_reading(s, PAUSE_REASON_SPEED_LIMIT)
        threading.Timer(delay_time, self.resume_reading, args=(s, PAUSE_REASON_SPEED_LIMIT)).start()

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
                    if key.data is None:
                        self._drain_wakeup()
                        continue
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
