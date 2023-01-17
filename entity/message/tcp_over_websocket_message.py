from typing_extensions import TypedDict


class TcpOverWebsocketMessage(TypedDict):
    uid: bytes  # 连接id
    name: str
    ip_port: str # ip和端口 127.0.0.1:8000
    data: bytes
