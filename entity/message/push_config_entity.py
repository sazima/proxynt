from typing import List, Optional

from typing_extensions import TypedDict


class ClientData(TypedDict):
    name: str
    remote_port: int
    local_port: int
    local_ip: str
    speed_limit: float
    protocol: str  # tcp or udp


class ClientToClientRule(TypedDict, total=False):
    """Client-to-client forwarding rule"""
    name: str                # Rule name
    target_client: str       # Target client name
    target_service: str      # Target service name (optional, for compatibility)
    target_ip: str           # Target IP address (optional, direct mode)
    target_port: int         # Target port (optional, direct mode)
    local_port: int          # Source client local listening port
    local_ip: str            # Source client local listening IP
    protocol: str            # tcp or udp
    speed_limit: float       # Speed limit


class PushConfigEntity(TypedDict):
    key: str
    version: str
    config_list: List[ClientData]  # 转发配置列表
    client_name: str  # 客户端名称
    client_to_client_rules: List[ClientToClientRule]  # C2C 转发规则（可选）
