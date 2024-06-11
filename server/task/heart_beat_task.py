import asyncio
import time
import traceback

from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from constant.message_type_constnat import MessageTypeConstant
from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity
from server.websocket_handler import MyWebSocketaHandler


class HeartBeatTask:
    def __init__(self, loop, break_time):
        self.loop = loop
        self.break_time = break_time

    def run(self):
        while True:
            time.sleep(self.break_time)
            try:
                self.send_heart_beat()
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
            try:
                self.check_recv_heart_beat_time()
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())

    def send_heart_beat(self):
        asyncio.set_event_loop(self.loop)
        # LoggerFactory.get_logger().debug('send heart beat')
        client_name_to_handler = MyWebSocketaHandler.client_name_to_handler
        ping_message: MessageEntity = {
            'type_': MessageTypeConstant.PING,
            'data': None
        }

        for h in client_name_to_handler.values():
            try:
                asyncio.ensure_future(
                    h.write_message(NatSerialization.dumps(ping_message, ContextUtils.get_password(), h.compress_support), binary=True))
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
        self.check_recv_heart_beat_time()

    def check_recv_heart_beat_time(self):
        """超时关闭"""
        asyncio.set_event_loop(self.loop)
        client_name_to_handler = MyWebSocketaHandler.client_name_to_handler
        for _, h in client_name_to_handler.items():
            t = h.recv_time
            if (time.time() - t) > SystemConstant.MAX_HEART_BEAT_SECONDS:
                LoggerFactory.get_logger().info(f'receive heart timeout {time.time() - t}, close client ')
                try:
                    h.close()
                except Exception:
                    LoggerFactory.get_logger().error('close error')
