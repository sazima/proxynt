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
    p2p_enabled: bool        # Whether to enable P2P for this rule (default: True)


class PushConfigEntity(TypedDict, total=False):
    key: str
    version: str
    config_list: List[ClientData]  # Forward configuration list
    client_name: str  # Client name
    client_to_client_rules: List[ClientToClientRule]  # C2C forward rules (optional)
    p2p_supported: bool  # Whether client supports P2P (optional)
    public_ip: str  # Client's public IP (filled by server, optional)
    public_port: int  # Client's public port (filled by server, optional)
    protocol_version: int  # Serialization protocol version (1=binary, 2=msgpack)
