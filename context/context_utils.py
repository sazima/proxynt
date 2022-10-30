c = {}


class ContextUtils:
    _websocket_path = '_websocket_path'
    _log_level = '_log_level'
    _port = '_port'
    _password_key = '_password_key'
    _log_path = '_log_path'

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
    def get_websocket_path(cls):
        return c[cls._websocket_path]

    @classmethod
    def set_log_file(cls, path):
        c[cls._log_path] = path

    @classmethod
    def get_log_file(cls) -> str:
        return c[cls._log_path]
