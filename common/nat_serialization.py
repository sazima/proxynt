import json
import os
import struct
import time

from common.encrypt_utils import EncryptUtils
from constant.message_type_constnat import MessageTypeConstant
from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity
from entity.message.tcp_over_websocket_message import TcpOverWebsocketMessage
from exceptions.replay_error import ReplayError
from exceptions.signature_error import SignatureError

UID_LEN = 4
HEADER_LEN = 38
EMPTY = bytes([0 for x in range(8)])


class NatSerialization:
    """
        header + body

        header (38 字节):
        类型(1字节) | body长度(4字节) | 随机字符串(5字节) | 时间戳(4字节) | 签名 (16 字节) | 空白(8字节)
        body (长度不固定):
        实际数据

    """

    # 报文形式: 类型, 数据
    @classmethod
    def dumps(cls, data: MessageEntity, key: str, encrypt_data: bool = True) -> bytes:
        type_ = data['type_']
        if type_ in (MessageTypeConstant.WEBSOCKET_OVER_TCP, MessageTypeConstant.REQUEST_TO_CONNECT):
            data_content: TcpOverWebsocketMessage = data['data']
            uid = data_content['uid']  # 长度r
            name = data_content['name']
            bytes_ = data_content['data']
            ip_port = data_content['ip_port']
            body = struct.pack(f'BBI{UID_LEN}s{len(name.encode())}s{len(ip_port)}s{len(bytes_)}s', len(name.encode()), len(ip_port), len(bytes_), uid, name.encode(), ip_port.encode(), bytes_)

        elif type_ == MessageTypeConstant.PUSH_CONFIG:
            body =  json.dumps(data).encode()
        elif type_ == MessageTypeConstant.PING:
            body =  b''
        else:
            body =  b'error'
        body_len = len(body)
        nonce = os.urandom(5)
        timestamp = struct.pack('I', int(time.time()))
        signature = EncryptUtils.md5_hash(nonce + timestamp + body[:12] + key.encode())
        header = type_.encode() + struct.pack('I', body_len) + nonce + timestamp + signature + EMPTY
        b =  header + body
        return EncryptUtils.encrypt(b, key)

    @classmethod
    def check_signature(cls, clear_text: bytes, data_len: int, key: str) -> bool:
        # return True
        nonce_and_timestamp = clear_text[5:14]
        body = clear_text[HEADER_LEN: data_len + HEADER_LEN]
        signature = clear_text[14:30]
        return signature == EncryptUtils.md5_hash(nonce_and_timestamp + body[:12] + key.encode())

    @classmethod
    def check_nonce_and_timestamp(cls, clear_text: bytes) -> bool:
        nonce = clear_text[5:10]
        timestamp = struct.unpack('I', clear_text[10:14])[0]
        nonce_to_time = ContextUtils.get_nonce_to_time()
        if nonce in nonce_to_time :
            return False
        # if nonce in nonce_to_time or time.time() - timestamp > SystemConstant.MAX_TIME_DIFFERENCE: # 因为物联网设备的时间不一定准 所以先不校验
        #     return False
        nonce_to_time[nonce] = int(time.time())
        return True

    @classmethod
    def loads(cls, byte_data: bytes, key: str) -> MessageEntity:
        byte_data = EncryptUtils.decrypt(byte_data, key)
        type_ = byte_data[0:1]
        body_len = struct.unpack('I', byte_data[1:5])[0]
        header = byte_data[:HEADER_LEN]
        if not cls.check_nonce_and_timestamp(byte_data):
            raise ReplayError()
        if not cls.check_signature(byte_data, body_len, key):
            raise SignatureError()
        body = byte_data[HEADER_LEN: body_len + HEADER_LEN]
        if type_.decode() in (MessageTypeConstant.WEBSOCKET_OVER_TCP, MessageTypeConstant.REQUEST_TO_CONNECT):
            len_name, len_ip_port, len_bytes = struct.unpack('BBI', body[:8])
            uid, name, ip_port,  socket_dta = struct.unpack(f'4s{len_name}s{len_ip_port}s{len_bytes}s', body[8:])
            data: TcpOverWebsocketMessage = {
                'uid': uid,
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
            return_data: MessageEntity = json.loads(body.decode())
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

    ContextUtils.set_nonce_to_time({})
    def _print_commend(msg):
        print(''.join("b'{}'".format(''.join('\\x{:02x}'.format(b) for b in msg))))


    data = {'type_': '5',
            'data': {'name': 'ssh',
                     'data': b'SSH-2.0-OpenSSH_7.8\r\n' ,
                     'uid': os.urandom(4),
                     'ip_port': '127.0.0.1:8888'}}
    key32 = 'xxxx'
    # salt = '!%F=-?Pst970'
    a = NatSerialization.dumps(data, key32)
    print(a)
    b = NatSerialization.loads(a, key32)
    print(b)
