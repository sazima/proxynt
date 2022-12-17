from typing import Dict, List

from typing_extensions import TypedDict

class _ClientEntity(TypedDict):
    name: str
    remote_port: int
    local_port: int
    local_ip: str


class ServerConfigEntity(TypedDict):
    port: int
    password: str
    path: str
    log_file: str

    client_config: Dict[str, List[_ClientEntity]]  # 在服务端配置的
