import json
import struct

from common.encrypt_utils import EncryptUtils
from constant.message_type_constnat import MessageTypeConstant
from entity.message.message_entity import MessageEntity
from entity.message.tcp_over_websocket_message import TcpOverWebsocketMessage


# todo 加 nonce 和 timestmap
class NatSerialization:
    """
        定义协议:
        字节:  0   | 1   -  4 |  5  -
      说明:  类型  |  报文长度  |   报文详情
    """

    # 报文形式: 类型, 数据
    @classmethod
    def dumps(cls, data: MessageEntity, key: str) -> bytes:
        type_ = data['type_']
        if type_ in (MessageTypeConstant.WEBSOCKET_OVER_TCP, MessageTypeConstant.REQUEST_TO_CONNECT):
            data_content: TcpOverWebsocketMessage = data['data']
            uid = data_content['uid']  # 长度32
            name = data_content['name']
            bytes_ = data_content['data']
            ip_port = data_content['ip_port']
            # I是uint32, 占4个字节, unsigned __int32	0 到 4,294,967,295;  uid是固定32
            # len: name: char 长度1
            # len: ip_port: char 长度1
            # len: bytess: uint32 长度4
            # b = type_.encode() + struct.pack(f'BBI32s{len(name.encode())}s{len(ip_port)}s{len(bytes_)}s', len(name.encode()), len(ip_port), len(bytes_),  uid.encode(), name.encode(),  ip_port.encode(), bytes_)
            b_data = struct.pack(f'BBI32s{len(name.encode())}s{len(ip_port)}s{len(bytes_)}s', len(name.encode()), len(ip_port), len(bytes_),  uid.encode(), name.encode(),  ip_port.encode(), bytes_)

        elif type_ == MessageTypeConstant.PUSH_CONFIG:
            # b = type_.encode() + json.dumps(data).encode()
            b_data =  json.dumps(data).encode()
            # b = type_.encode() + json.dumps(data).encode()
        elif type_ == MessageTypeConstant.PING:
            b_data =  b''
            # b = MessageTypeConstant.PING.encode()
        else:
            b_data =  b'error'
        b_data_len = len(b_data)
        b = type_.encode()  + struct.pack('I', b_data_len) + b_data
        return EncryptUtils.encrypt(b, key)

    @classmethod
    def loads(cls, byte_data: bytes, key: str) -> MessageEntity:
        byte_data = EncryptUtils.decrypt(byte_data, key)
        type_ = byte_data[0:1]
        data_len = struct.unpack('I', byte_data[1:5])[0]
        byte_data = byte_data[0:data_len + 5]  #
        if type_.decode() in (MessageTypeConstant.WEBSOCKET_OVER_TCP, MessageTypeConstant.REQUEST_TO_CONNECT):
            # I是uint32, 占4个字节, unsigned __int32	0 到 4,294,967,295;  uid是固定32
            len_name, len_ip_port, len_bytes = struct.unpack('BBI', byte_data[5:13])
            uid, name, ip_port,  socket_dta = struct.unpack(f'32s{len_name}s{len_ip_port}s{len_bytes}s', byte_data[13:])
            data: TcpOverWebsocketMessage = {
                'uid': uid.decode(),
                'name': name.decode(),
                'ip_port': ip_port.decode(),
                'data': socket_dta
            }
            return_data: MessageEntity = {
                'type_': type_.decode(),
                'data': data
            }
            return return_data
        elif type_ == MessageTypeConstant.PUSH_CONFIG.encode():
            return_data: MessageEntity = json.loads(byte_data[5:].decode())
            return return_data
        elif type_ == MessageTypeConstant.PING.encode():
            return_data: MessageEntity = {
                'type_': type_.decode(),
                'data': None
            }
            return return_data
        else:
            raise Exception('error ')



if __name__ == '__main__':

    def _print_commend(msg):
        print(''.join("b'{}'".format(''.join('\\x{:02x}'.format(b) for b in msg))))


    data = {'type_': '5',
            'data': {'name': 'ssh',
                     'data': b'SSH-2.0-OpenSSH_7.8\r\n' ,
                     'uid': 'e18fe62fa05f446db95236c9826bfdd6 ',
                     'ip_port': '127.0.0.1:8888'}}
    key32 = 'xxxx'
    # salt = '!%F=-?Pst970'
    a = NatSerialization.dumps(data, key32)
    print(a)
    b = NatSerialization.loads(a, key32)
    print(b)
