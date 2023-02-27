from typing import List

from typing_extensions import TypedDict


class ClientData(TypedDict):
    name: str
    remote_port: int
    local_port: int
    local_ip: str
    speed_limit: float


class PushConfigEntity(TypedDict):
    key: str
    version: str
    config_list: List[ClientData]  # 转发配置列表
    client_name: str  # 客户端名称
