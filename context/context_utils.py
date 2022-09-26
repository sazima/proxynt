c = {}


class ContextUtils:
    _port = '_port'
    _password_key = '_password_key'

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
