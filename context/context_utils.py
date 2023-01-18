from typing import List, Dict

from entity.message.push_config_entity import ClientData
from entity.server_config_entity import AdminEntity

c = {}


class ContextUtils:
    _cookie_to_time = '_cookie_to_time'
    _admin_config = '_admin_config'
    _websocket_path = '_websocket_path'
    _log_level = '_log_level'
    _port = '_port'
    _password_key = '_password_key'
    _log_path = '_log_path'
    _config_file_path = '_config_file_path'
    _client_name_to_config_in_server = '_client_name_to_config_in_server'
    _nonce_to_timestamp = '_nonce_to_timestamp'

    @classmethod
    def get_password(cls) -> str:
        return c.get(cls._password_key)

    @classmethod
    def set_password(cls, data: str):
        c[cls._password_key] = data

    @classmethod
    def set_port(cls, data):
        c[cls._port] = data

    @classmethod
    def get_port(cls):
        return c[cls._port]

    @classmethod
    def set_log_level(cls, data):
        c[cls._log_level] = data

    @classmethod
    def get_log_level(cls) -> int:
        return c[cls._log_level]

    @classmethod
    def set_websocket_path(cls, path):
        c[cls._websocket_path] = path

    @classmethod
    def get_websocket_path(cls) -> str:
        return c[cls._websocket_path]

    @classmethod
    def set_config_file_path(cls, path):
        c[cls._config_file_path] = path
    @classmethod
    def get_config_file_path(cls) -> str:
        return c[cls._config_file_path]

    @classmethod
    def set_log_file(cls, path):
        c[cls._log_path] = path

    @classmethod
    def get_log_file(cls) -> str:
        return c[cls._log_path]

    @classmethod
    def set_client_name_to_config_in_server(cls, data):
        c[cls._client_name_to_config_in_server] = data

    @classmethod
    def get_client_name_to_config_in_server(cls) -> Dict[str, List[ClientData]]:
        """在server端添加的配置"""
        return c[cls._client_name_to_config_in_server]
    @classmethod
    def get_admin_config(cls) -> AdminEntity:
        return c.get(cls._admin_config)

    @classmethod
    def set_admin_config(cls, data):
        c[cls._admin_config] = data


    @classmethod
    def get_cookie_to_time(cls) -> Dict[str, float]:
        return c[cls._cookie_to_time]

    @classmethod
    def set_cookie_to_time(cls, data:  Dict[str, float]):
        c[cls._cookie_to_time] = data

    @classmethod
    def get_nonce_to_time(cls) -> Dict[bytes, int]:
        return c[cls._nonce_to_timestamp]

    @classmethod
    def set_nonce_to_time(cls, data:  Dict[bytes, int]):
        c[cls._nonce_to_timestamp] = data
