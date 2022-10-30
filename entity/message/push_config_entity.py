from typing import List

from typing_extensions import TypedDict


class ClientData(TypedDict):
    name: str
    remote_port: int
    local_port: int
    local_ip: str


class PushConfigEntity(TypedDict):
    key: str
    config_list: List[ClientData]
