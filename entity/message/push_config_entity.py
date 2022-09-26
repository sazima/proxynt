from typing_extensions import TypedDict


class PushConfigEntity(TypedDict):
    name: str
    remote_port: int
    local_port: int
    local_ip: str
