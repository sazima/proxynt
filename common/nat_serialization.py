import json
import struct

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
            # I是uint32, 占4位, uid是固定32
            b = type_.encode() + struct.pack(f'II32s{len(name.encode())}s{len(bytes_)}s', len(name.encode()), len(bytes_), uid.encode(), name.encode(),  bytes_)
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
            # I是uint32, 占4位, uid是固定32
            len_name, len_bytes = struct.unpack('II', byte_data[1:9])
            uid, name, socket_dta = struct.unpack(f'32s{len_name}s{len_bytes}s', byte_data[9:])
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

    data = {'type_': '2',
            'data': {'name': 'ssh',
                     'data': b'SSH-2.0-OpenSSH_7.8\r\n',
                     'uid': 'e18fe62fa05f446db95236c9826bfdd6'}}
    a = NatSerialization.dumps(data, '')
    # print(pickle.dumps(a))
    print(a)
    b = NatSerialization.loads(a, '')
    print(b)
