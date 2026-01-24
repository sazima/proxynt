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
        # 智能心跳：记录每个客户端最后业务活动时间
        self.last_business_activity = {}  # client_name -> timestamp
        self.heartbeat_skip_threshold = 10  # 10秒内有业务活动则跳过心跳

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

    def update_business_activity(self, client_name: str):
        """更新业务活动时间（由 websocket_handler 调用）"""
        self.last_business_activity[client_name] = time.time()

    def send_heart_beat(self):
        asyncio.set_event_loop(self.loop)
        # LoggerFactory.get_logger().debug('send heart beat')
        client_name_to_handler = MyWebSocketaHandler.client_name_to_handler
        ping_message: MessageEntity = {
            'type_': MessageTypeConstant.PING,
            'data': None
        }

        now = time.time()
        for client_name, h in client_name_to_handler.items():
            try:
                # 智能心跳：如果最近有业务活动，跳过心跳发送
                last_activity = self.last_business_activity.get(client_name, 0)
                if now - last_activity < self.heartbeat_skip_threshold:
                    # LoggerFactory.get_logger().debug(f'skip heartbeat for {client_name}, recent activity: {now - last_activity:.1f}s ago')
                    continue

                asyncio.ensure_future(
                    h.write_message(NatSerialization.dumps(ping_message, ContextUtils.get_password(), h.compress_support, h.protocol_version), binary=True))
            except Exception:
                LoggerFactory.get_logger().error(traceback.format_exc())
        self.check_recv_heart_beat_time()

    def check_recv_heart_beat_time(self):
        """超时关闭"""
        asyncio.set_event_loop(self.loop)
        client_name_to_handler = MyWebSocketaHandler.client_name_to_handler
        for client_name, h in client_name_to_handler.items():
            last_activity = self.last_business_activity.get(client_name, 0)
            _t = h.recv_time
            t = max([last_activity, _t])
            if (time.time() - t) > SystemConstant.MAX_HEART_BEAT_SECONDS:
                LoggerFactory.get_logger().info(f'receive heart timeout {time.time() - t}, close client ')
                try:
                    h.close()
                except Exception:
                    LoggerFactory.get_logger().error('close error')
