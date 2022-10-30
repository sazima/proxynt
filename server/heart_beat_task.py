import asyncio
from common.logger_factory import LoggerFactory
from common.nat_serialization import NatSerialization
from constant.message_type_constnat import MessageTypeConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity
from server.websocket_handler import MyWebSocketaHandler


class HeartBeatTask:
    def run(self):
        LoggerFactory.get_logger().debug('send heart beat')
        handler_to_names = MyWebSocketaHandler.handler_to_names
        ping_message: MessageEntity = {
            'type_': MessageTypeConstant.PING,
            'data': None
        }

        for h in handler_to_names.keys():
            asyncio.ensure_future(h.write_message(NatSerialization.dumps(ping_message, ContextUtils.get_password())))


