from typing import List, Union

from typing_extensions import TypedDict

from entity.message.push_config_entity import PushConfigEntity
from entity.message.tcp_over_websocket_message import TcpOverWebsocketMessage


class MessageEntity(TypedDict):
    type_: str
    data: Union[PushConfigEntity, TcpOverWebsocketMessage, None]
