"""
NatSerialization V2 - msgpack format (flexible, supports arbitrary fields)
"""
import os
import struct
import time

import msgpack

try:
    import snappy
    has_snappy = True
except ModuleNotFoundError:
    has_snappy = False

from common.encrypt_utils import EncryptUtils
from entity.message.message_entity import MessageEntity

HEADER_LEN = 22  # xxHash64 模式: 22 字节


class NatSerializationV2:
    """
    V2 序列化格式 - msgpack 格式

    header + body

    header (22 字节) - 使用 xxHash64 签名:
    类型(1字节) | body长度(4字节) | 随机字符串(5字节) | 时间戳(4字节) | 签名 (8 字节)

    body (长度不固定):
    msgpack 编码的数据，自动支持所有字段
    """

    @classmethod
    def dumps(cls, data: MessageEntity, key: str, compress: bool) -> bytes:
        type_ = data['type_']
        data_content = data.get('data')

        # 处理 data_content，确保可以被 msgpack 序列化
        if data_content is not None:
            # 复制一份避免修改原数据
            # serializable_content = dict(data_content) if isinstance(data_content, dict) else data_content
            serializable_content = data_content

            # 如果有 'data' 字段且需要压缩
            if isinstance(serializable_content, dict) and 'data' in serializable_content:
                if compress and has_snappy and serializable_content['data']:
                    serializable_content['data'] = snappy.snappy.compress(serializable_content['data'])
                    serializable_content['_compressed'] = True

            body = msgpack.packb(serializable_content, use_bin_type=True)
        else:
            body = b''

        body_len = len(body)
        nonce = os.urandom(5)
        timestamp = struct.pack('I', int(time.time()))
        signature = EncryptUtils.xxhash64_hash(nonce + timestamp + body[:12] + key.encode())
        header = type_.encode() + struct.pack('I', body_len) + nonce + timestamp + signature
        b = header + body
        return EncryptUtils.encrypt(b, key)

    @classmethod
    def check_signature(cls, clear_text: bytes, data_len: int, key: str) -> bool:
        nonce_and_timestamp = clear_text[5:14]
        body = clear_text[HEADER_LEN: data_len + HEADER_LEN]
        signature = clear_text[14:22]
        return signature == EncryptUtils.xxhash64_hash(nonce_and_timestamp + body[:12] + key.encode())

    @classmethod
    def check_nonce_and_timestamp(cls, clear_text: bytes) -> bool:
        return True

    @classmethod
    def loads(cls, byte_data: bytes, key: str, compress: bool) -> MessageEntity:
        byte_data = EncryptUtils.decrypt(byte_data, key)
        type_ = byte_data[0:1]
        body_len = struct.unpack('I', byte_data[1:5])[0]

        if not cls.check_nonce_and_timestamp(byte_data):
            from exceptions.replay_error import ReplayError
            raise ReplayError()
        if not cls.check_signature(byte_data, body_len, key):
            from exceptions.signature_error import SignatureError
            print(f'SignatureError: {key}')
            raise SignatureError()

        body = byte_data[HEADER_LEN: body_len + HEADER_LEN]

        if body:
            data_content = msgpack.unpackb(body, raw=False)

            # 如果数据被压缩过，解压
            if isinstance(data_content, dict):
                if data_content.get('_compressed') and 'data' in data_content:
                    if has_snappy and data_content['data']:
                        data_content['data'] = snappy.snappy.uncompress(data_content['data'])
                    del data_content['_compressed']
        else:
            data_content = None

        return_data: MessageEntity = {
            'type_': type_.decode(),
            'data': data_content
        }
        return return_data
