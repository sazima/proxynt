import asyncio
import threading
import time
import traceback

from tornado import ioloop

from common import websocket
from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from common.websocket import WebSocketConnectionClosedException
from constant.message_type_constnat import MessageTypeConstant
from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class HeatBeatTask:
    def __init__(self, ws: websocket.WebSocketApp):
        self.ws: websocket.WebSocketApp = ws
        self.is_running = False
        self.recv_heart_beat_time: float = time.time()
        self.lock = asyncio.Lock()

    def set_recv_heart_beat_time(self, d: float):
        self.recv_heart_beat_time = d

    async def run(self):
        if self.lock.locked():
            LoggerFactory.get_logger().error('locked return')
            return
        async with self.lock:
            try:
                await asyncio.wait_for(ioloop.IOLoop.current().run_in_executor(None, self.send_heart_beat), timeout=20)
            except WebSocketConnectionClosedException:
                try:
                    await asyncio.wait_for(ioloop.IOLoop.current().run_in_executor(None, self._close_and_on_close), timeout=20)
                except Exception:
                    LoggerFactory.get_logger().error(traceback.format_exc())
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
            self.check_recv_heart_beat_time()

    def send_heart_beat(self):
        if self.is_running:
            # LoggerFactory.get_logger().debug('start send heart beat  ')
            ping_message: MessageEntity = {
                'type_': MessageTypeConstant.PING,
                'data': None
            }
            self.ws.send(NatSerialization.dumps(ping_message, ContextUtils.get_password()), websocket.ABNF.OPCODE_BINARY)
            # LoggerFactory.get_logger().debug('send client heart beat success ')
        else:
            pass
            # LoggerFactory.get_logger().debug('not running , skip send heart beat ')

    def check_recv_heart_beat_time(self):
        """超时关闭"""
        if self.is_running:
            # LoggerFactory.get_logger().debug('time %s ', self.recv_heart_beat_time)
            if (time.time() - self.recv_heart_beat_time) > SystemConstant.MAX_HEART_BEAT_SECONDS:
                LoggerFactory.get_logger().info(f'receive heart timeout {time.time() - self.recv_heart_beat_time}, close client  ')
                self.ws.close()  # 有时候不会自己调用on_close 方法
        else:
            pass
            LoggerFactory.get_logger().debug('not running , skip check recv heart beat ')

    def _close_and_on_close(self):
        self.ws.close()
        self.ws.on_close(self.ws, None, None)


