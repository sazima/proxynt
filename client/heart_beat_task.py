import traceback

import websocket

from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity


class HeatBeatTask:
    def __init__(self, ws: websocket.WebSocketApp):
        self.ws: websocket.WebSocketApp = ws
        self.is_running = False

    def run(self):
        try:
            if self.is_running:
                ping_message: MessageEntity = {
                    'type_': MessageTypeConstant.PING,
                    'data': None
                }
                self.ws.send(NatSerialization.dumps(ping_message, ContextUtils.get_password()), websocket.ABNF.OPCODE_BINARY)
                LoggerFactory.get_logger().debug('send client heart beat success ')
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

