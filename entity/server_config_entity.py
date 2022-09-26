from typing_extensions import TypedDict


class ServerConfigEntity(TypedDict):
    port: int
    password: str
