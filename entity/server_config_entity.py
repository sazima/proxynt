from typing import Dict, List

from typing_extensions import TypedDict

class _ClientEntity(TypedDict):
    name: str
    remote_port: int
    local_port: int
    local_ip: str


class _ClientToClientRule(TypedDict, total=False):
    name: str
    source_client: str
    target_client: str
    target_service: str      # Target service name (optional, for compatibility)
    target_ip: str           # Target IP address (optional, direct mode)
    target_port: int         # Target port (optional, direct mode)
    local_port: int
    local_ip: str
    protocol: str
    speed_limit: float
    enabled: bool
    p2p_enabled: bool        # Whether to enable P2P for this rule (default: True)


class AdminEntity(TypedDict):
    enable: bool
    admin_password: str


class ServerConfigEntity(TypedDict):
    port: int
    password: str
    path: str
    log_file: str
    admin: AdminEntity

    default_expand_all: bool

    client_config: Dict[str, List[_ClientEntity]]  # 在服务端配置的
    client_to_client_rules: List[_ClientToClientRule]  # 客户端到客户端转发规则
