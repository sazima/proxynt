from typing import List, Optional

from typing_extensions import TypedDict

from entity.message.push_config_entity import ClientData


class _ServerEntity(TypedDict):
    url: Optional[str]  #
    port: int
    host: str

    https: bool
    password: str
    path: str
    compress: bool


class ClientConfigEntity(TypedDict):
    server: _ServerEntity
    client: List[ClientData]
    log_file: str
    client_name: str
