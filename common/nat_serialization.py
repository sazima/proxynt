import json

from common.encrypt_utils import EncryptUtils
from constant.message_type_constnat import MessageTypeConstant
from entity.message.message_entity import MessageEntity
from entity.message.tcp_over_websocket_message import TcpOverWebsocketMessage


class NatSerialization:

    # 报文形式: 类型, 数据
    # 如果是config的: name长度, name, remote_port长度, remote_port, local_port长度, local_port, local_ip长度, local_ip
    # 如果是socket数据: 后面是  uid:[32位], name长度, name, 报文
    @classmethod
    def dumps(cls, data: MessageEntity, key: str) -> bytes:
        b = b''
        type_ = data['type_']
        b += type_.encode()
        if type_ == MessageTypeConstant.WEBSOCKET_OVER_TCP:
            data_content: TcpOverWebsocketMessage = data['data']
            uid = data_content['uid']  # 长度32
            name = data_content['name']
            bytes_ = data_content['data']
            b += f'{uid}' \
                 f'{str(len(name.encode())).zfill(3)}{name}'.encode() + bytes_
        elif type_ == MessageTypeConstant.PUSH_CONFIG:
            b = type_.encode() + json.dumps(data).encode()
        elif type_ == MessageTypeConstant.PING:
            b = MessageTypeConstant.PING.encode()
        return EncryptUtils.encode(b, key)

    @classmethod
    def loads(cls, byte_data: bytes, key: str) -> MessageEntity:
        byte_data = EncryptUtils.decode(byte_data, key)
        type_ = byte_data[0:1]
        if type_ == MessageTypeConstant.WEBSOCKET_OVER_TCP.encode():
            start = 1
            uid = byte_data[start: start + 32].decode()
            start += 32
            name_len = int(byte_data[start: start + 3].decode())
            start += 3
            name = byte_data[start: start + name_len].decode()
            start += name_len
            socket_dta: bytes = byte_data[start:]
            data: TcpOverWebsocketMessage = {
                'uid': uid,
                'name': name,
                'data': socket_dta
            }
            return_data: MessageEntity = {
                'type_': type_.decode(),
                'data': data
            }
            return return_data
        elif type_ == MessageTypeConstant.PUSH_CONFIG.encode():
            return_data: MessageEntity = json.loads(byte_data[1:].decode())
            return return_data
        elif type_ == MessageTypeConstant.PING.encode():
            return_data: MessageEntity = {
                'type_': type_.decode(),
                'data': None
            }
            return return_data



if __name__ == '__main__':
    data = {
        'type_': '1',
        'data': [
                {
                    "name": "ssh",
                    "remote_port": 1222,
                    "local_port": 22,
                    "local_ip": "127.0.0.1"
                },
                {
                    "name": "mongo",
                    "remote_port": 1223,
                    "local_port": 27017,
                    "local_ip": "127.0.0.1"
                }
            ]

    }
    data = {'type_': '2',
            'data': {'name': 'ssh',
                     'data': b'SSH-2.0-OpenSSH_7.8\r\n',
                     'uid': 'e18fe62fa05f446db95236c9826bfdd6'}}
    a = NatSerialization.dumps(data)
    # print(pickle.dumps(a))
    print(a)
    b = NatSerialization.loads(a)
    print(b)
