from typing import List

from typing_extensions import TypedDict


class ClientData(TypedDict):
    name: str
    remote_port: int
    local_port: int
    local_ip: str
    speed_limit: float
    protocol: str  # tcp or udp


class ClientToClientRule(TypedDict):
    """客户端到客户端转发规则"""
    name: str                # 规则名称
    target_client: str       # 目标客户端名称
    target_service: str      # 目标服务名称
    local_port: int          # 源客户端本地监听端口
    local_ip: str            # 源客户端本地监听 IP
    protocol: str            # tcp or udp
    speed_limit: float       # 速度限制


class PushConfigEntity(TypedDict):
    key: str
    version: str
    config_list: List[ClientData]  # 转发配置列表
    client_name: str  # 客户端名称
    client_to_client_rules: List[ClientToClientRule]  # C2C 转发规则（可选）
