import json
import os
import struct
import time

try:
    import snappy
    has_snappy = True
except ModuleNotFoundError:
    has_snappy = False

from common.encrypt_utils import EncryptUtils
from constant.message_type_constnat import MessageTypeConstant
from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity
from entity.message.tcp_over_websocket_message import TcpOverWebsocketMessage
from exceptions.replay_error import ReplayError
from exceptions.signature_error import SignatureError

UID_LEN = 4
HEADER_LEN = 22  # xxHash64 模式: 22 字节（比 MD5 模式节省 42%）


class NatSerialization:
    """
        header + body

        header (22 字节) - 使用 xxHash64 签名:
        类型(1字节) | body长度(4字节) | 随机字符串(5字节) | 时间戳(4字节) | 签名 (8 字节)

        body (长度不固定):
        实际数据

        注：使用 xxHash64 替代 MD5，签名速度提升 10 倍，头部大小减少 42%
    """

    # 报文形式: 类型, 数据
    @classmethod
    def dumps(cls, data: MessageEntity, key: str, compress: bool) -> bytes:
        type_ = data['type_']
        if type_ in (MessageTypeConstant.WEBSOCKET_OVER_TCP, MessageTypeConstant.REQUEST_TO_CONNECT,
                     MessageTypeConstant.WEBSOCKET_OVER_UDP, MessageTypeConstant.REQUEST_TO_CONNECT_UDP,
                     MessageTypeConstant.CONNECT_CONFIRMED, MessageTypeConstant.CONNECT_FAILED):  # 添加所有消息类型
            data_content: TcpOverWebsocketMessage = data['data']
            uid = data_content['uid']  # 长度r
            name = data_content['name']
            if compress:
                bytes_: TcpOverWebsocketMessage = snappy.snappy.compress(data_content['data'])
            else:
                bytes_ = data_content['data']
            ip_port = data_content['ip_port']
            body = struct.pack(f'BBI{UID_LEN}s{len(name.encode())}s{len(ip_port)}s{len(bytes_)}s', len(name.encode()), len(ip_port), len(bytes_), uid, name.encode(), ip_port.encode(), bytes_)

        elif type_ == MessageTypeConstant.CLIENT_TO_CLIENT_FORWARD:
            # C2C forward request: support two modes
            data_content = data['data']
            uid = data_content['uid']
            target_client = data_content['target_client'].encode()
            source_rule_name = data_content['source_rule_name'].encode()
            protocol = data_content['protocol'].encode()

            # Check if using direct mode (target_ip + target_port) or service mode (target_service)
            if 'target_ip' in data_content and 'target_port' in data_content:
                # Direct mode: magic(1) | mode_flag(1) | len_target_client(1) | len_target_ip(1) | len_source_rule_name(1) | len_protocol(1) | target_port(2) | uid(4) | strings...
                magic = 0xFF  # Magic number to identify new format
                mode_flag = 0x01
                target_ip = data_content['target_ip'].encode()
                target_port = data_content['target_port']
                body = struct.pack(f'BBBBBBH{UID_LEN}s{len(target_client)}s{len(target_ip)}s{len(source_rule_name)}s{len(protocol)}s',
                                 magic, mode_flag, len(target_client), len(target_ip), len(source_rule_name), len(protocol),
                                 target_port, uid, target_client, target_ip, source_rule_name, protocol)
            else:
                # Service mode: check if new format or old format for backward compatibility
                target_service = data_content['target_service'].encode()

                # Always use new format when sending (with magic number for identification)
                magic = 0xFF  # Magic number to identify new format
                mode_flag = 0x00
                body = struct.pack(f'BBBBBB{UID_LEN}s{len(target_client)}s{len(target_service)}s{len(source_rule_name)}s{len(protocol)}s',
                                 magic, mode_flag, len(target_client), len(target_service), len(source_rule_name), len(protocol),
                                 uid, target_client, target_service, source_rule_name, protocol)

        elif type_ == MessageTypeConstant.PUSH_CONFIG:
            body =  json.dumps(data).encode()
        elif type_ == MessageTypeConstant.PING:
            body =  b''
        elif type_ in (MessageTypeConstant.P2P_OFFER, MessageTypeConstant.P2P_ANSWER,
                      MessageTypeConstant.P2P_CANDIDATE, MessageTypeConstant.P2P_SUCCESS,
                      MessageTypeConstant.P2P_FAILED):
            # P2P messages: use JSON encoding of data content only
            data_content = data.get('data', {})
            body = json.dumps(data_content).encode()
        else:
            body =  b'error'
        body_len = len(body)
        nonce = os.urandom(5)
        timestamp = struct.pack('I', int(time.time()))
        # 使用 xxHash64 签名（8 字节，比 MD5 快 10 倍）
        signature = EncryptUtils.xxhash64_hash(nonce + timestamp + body[:12] + key.encode())
        header = type_.encode() + struct.pack('I', body_len) + nonce + timestamp + signature
        b =  header + body
        return EncryptUtils.encrypt(b, key)

    @classmethod
    def check_signature(cls, clear_text: bytes, data_len: int, key: str) -> bool:
        nonce_and_timestamp = clear_text[5:14]
        body = clear_text[HEADER_LEN: data_len + HEADER_LEN]
        signature = clear_text[14:22]  # xxHash64 签名 8 字节
        return signature == EncryptUtils.xxhash64_hash(nonce_and_timestamp + body[:12] + key.encode())

    @classmethod
    def check_nonce_and_timestamp(cls, clear_text: bytes) -> bool:
        return True
        # check and Anti replay attack
        # nonce = clear_text[5:10]
        # timestamp = struct.unpack('I', clear_text[10:14])[0]
        # nonce_to_time = ContextUtils.get_nonce_to_time()
        # if nonce in nonce_to_time :
        #     return False
        # nonce_to_time[nonce] = int(time.time())
        # return True

    @classmethod
    def loads(cls, byte_data: bytes, key: str, compress: bool) -> MessageEntity:
        byte_data = EncryptUtils.decrypt(byte_data, key)
        type_ = byte_data[0:1]
        body_len = struct.unpack('I', byte_data[1:5])[0]
        header = byte_data[:HEADER_LEN]
        if not cls.check_nonce_and_timestamp(byte_data):
            raise ReplayError()
        if not cls.check_signature(byte_data, body_len, key):
            print(f'SignatureError: {key}')
            raise SignatureError()
        body = byte_data[HEADER_LEN: body_len + HEADER_LEN]
        if type_.decode() in (MessageTypeConstant.WEBSOCKET_OVER_TCP, MessageTypeConstant.REQUEST_TO_CONNECT,
                              MessageTypeConstant.WEBSOCKET_OVER_UDP, MessageTypeConstant.REQUEST_TO_CONNECT_UDP,
                              MessageTypeConstant.CONNECT_CONFIRMED, MessageTypeConstant.CONNECT_FAILED):  # 添加所有消息类型
            len_name, len_ip_port, len_bytes = struct.unpack('BBI', body[:8])
            uid, name, ip_port,  socket_dta = struct.unpack(f'4s{len_name}s{len_ip_port}s{len_bytes}s', body[8:])
            if compress and len(socket_dta):
                socket_dta = snappy.snappy.uncompress(socket_dta)
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
        elif type_.decode() == MessageTypeConstant.CLIENT_TO_CLIENT_FORWARD:
            # Parse C2C forward request: support old and new formats
            first_byte = struct.unpack('B', body[:1])[0]

            if first_byte == 0xFF:
                # New format: magic(1) | mode_flag(1) | ...
                mode_flag = struct.unpack('B', body[1:2])[0]

                if mode_flag == 0x01:
                    # Direct mode: parse target_ip and target_port
                    len_target_client, len_target_ip, len_source_rule_name, len_protocol = struct.unpack('BBBB', body[2:6])
                    target_port = struct.unpack('H', body[6:8])[0]
                    uid, target_client, target_ip, source_rule_name, protocol = struct.unpack(
                        f'4s{len_target_client}s{len_target_ip}s{len_source_rule_name}s{len_protocol}s', body[8:])
                    data = {
                        'uid': uid,
                        'target_client': target_client.decode(),
                        'target_ip': target_ip.decode(),
                        'target_port': target_port,
                        'source_rule_name': source_rule_name.decode(),
                        'protocol': protocol.decode()
                    }
                else:
                    # Service mode (new format): parse target_service
                    len_target_client, len_target_service, len_source_rule_name, len_protocol = struct.unpack('BBBB', body[2:6])
                    uid, target_client, target_service, source_rule_name, protocol = struct.unpack(
                        f'4s{len_target_client}s{len_target_service}s{len_source_rule_name}s{len_protocol}s', body[6:])
                    data = {
                        'uid': uid,
                        'target_client': target_client.decode(),
                        'target_service': target_service.decode(),
                        'source_rule_name': source_rule_name.decode(),
                        'protocol': protocol.decode()
                    }
            else:
                # Old format (backward compatible): len_target_client(1) | len_target_service(1) | len_source_rule_name(1) | len_protocol(1) | uid(4) | strings...
                len_target_client = first_byte
                len_target_service, len_source_rule_name, len_protocol = struct.unpack('BBB', body[1:4])
                uid, target_client, target_service, source_rule_name, protocol = struct.unpack(
                    f'4s{len_target_client}s{len_target_service}s{len_source_rule_name}s{len_protocol}s', body[4:])
                data = {
                    'uid': uid,
                    'target_client': target_client.decode(),
                    'target_service': target_service.decode(),
                    'source_rule_name': source_rule_name.decode(),
                    'protocol': protocol.decode()
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
        elif type_.decode() in (MessageTypeConstant.P2P_OFFER, MessageTypeConstant.P2P_ANSWER,
                                MessageTypeConstant.P2P_CANDIDATE, MessageTypeConstant.P2P_SUCCESS,
                                MessageTypeConstant.P2P_FAILED):
            # P2P messages: JSON decode
            data = json.loads(body.decode())
            return_data: MessageEntity = {
                'type_': type_.decode(),
                'data': data
            }
            return return_data
        else:
            raise Exception('error ')



if __name__ == '__main__':

    ContextUtils.set_nonce_to_time({})
    def _print_commend(msg):
        print(''.join("b'{}'".format(''.join('\\x{:02x}'.format(b) for b in msg))))


    data = {'type_': 'a',
            'data': {'name': 'ssh',
                     'target_client': 'abc',
                     'target_service': 'abc',
                     'source_rule_name': 'source_rule_name',
                     'protocol': 'tcp',
                     # 'data': 'SSH-2.0-OpenSSH_7.8'.encode() ,
                     'data': b'' ,
                     'uid': os.urandom(4),
                     'ip_port': '127.0.0.1:8888'}}
    key32 = 'xxxx'
    # salt = '!%F=-?Pst970'
    a = NatSerialization.dumps(data, key32, False)
    print(a)
    b = NatSerialization.loads(a, key32, True)
    print(b)
