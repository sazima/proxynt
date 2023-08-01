from typing import List

from typing_extensions import TypedDict

from entity.message.push_config_entity import ClientData


class _ServerEntity(TypedDict):
    port: int
    host: str

    https: bool
    password: str
    path: str


class ClientConfigEntity(TypedDict):
    server: _ServerEntity
    client: List[ClientData]
    log_file: str
    client_name: str
