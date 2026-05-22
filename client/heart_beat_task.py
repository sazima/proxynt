import logging
import time
import traceback

from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from constant.message_type_constnat import MessageTypeConstant
from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class HeatBeatTask:
    def __init__(self, ws_sender, sleep_break: int, protocol_version: int):
        self.ws_sender = ws_sender
        self.is_running = False
        self.recv_heart_beat_time: float = time.time()
        self.sleep_break = sleep_break
        self.protocol_version = protocol_version
        self._periodic_callback = None

    def set_recv_heart_beat_time(self, d: float):
        self.recv_heart_beat_time = d

    def start(self, io=None):
        """Start heartbeat PeriodicCallback on the IOLoop. No thread needed."""
        from tornado.ioloop import PeriodicCallback, IOLoop
        target_io = io or IOLoop.current()
        # PeriodicCallback interval is in milliseconds
        self._periodic_callback = PeriodicCallback(self._tick, self.sleep_break * 1000)
        self._periodic_callback.start()

    def stop(self):
        if self._periodic_callback is not None:
            self._periodic_callback.stop()
            self._periodic_callback = None

    def _tick(self):
        """Called by PeriodicCallback on the IOLoop."""
        if not self.is_running:
            return
        if LoggerFactory.get_logger().isEnabledFor(logging.DEBUG):
            LoggerFactory.get_logger().debug('run send heartbeat')
        try:
            self.send_heart_beat()
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())
        try:
            self.check_recv_heart_beat_time()
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def send_heart_beat(self):
        if not self.is_running:
            return
        ping_message: MessageEntity = {
            'type_': MessageTypeConstant.PING,
            'data': None
        }
        self.ws_sender.send(
            NatSerialization.dumps(ping_message, ContextUtils.get_password(), False, self.protocol_version)
        )

    def check_recv_heart_beat_time(self):
        """Close connection on heartbeat timeout."""
        if not self.is_running:
            return
        elapsed = time.time() - self.recv_heart_beat_time
        if elapsed > SystemConstant.MAX_HEART_BEAT_SECONDS:
            LoggerFactory.get_logger().info(
                'Heartbeat receive timeout %.1f s, closing connection' % elapsed
            )
            # Closing the Tornado ws triggers on_message(None) → on_close
            self.ws_sender.close()
