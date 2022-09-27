from typing_extensions import TypedDict


class TcpOverWebsocketMessage(TypedDict):
    uid: str  # 连接id
    name: str
    data: bytes
