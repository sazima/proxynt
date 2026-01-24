"""
NatSerialization - Version dispatcher

根据 protocol_version 选择对应的序列化实现：
- V1: 二进制格式（原始实现，兼容旧客户端）
- V2: msgpack 格式（灵活，支持任意字段）
"""
import os
import struct

from common.nat_serialization_v1 import NatSerializationV1
from common.nat_serialization_v2 import NatSerializationV2
from context.context_utils import ContextUtils
from entity.message.message_entity import MessageEntity

# 默认协议版本
DEFAULT_PROTOCOL_VERSION = 1

# 版本到序列化器的映射
VERSION_TO_SERIALIZER = {
    1: NatSerializationV1,
    2: NatSerializationV2,
}


class NatSerialization:
    """
    序列化入口类 - 根据版本分发到对应的实现

    使用方法：
    - dumps/loads 默认使用 V1（向后兼容）
    - 可以通过 protocol_version 参数指定版本
    """

    @classmethod
    def get_serializer(cls, protocol_version: int = DEFAULT_PROTOCOL_VERSION):
        """获取指定版本的序列化器"""
        serializer = VERSION_TO_SERIALIZER.get(protocol_version)
        if serializer is None:
            raise ValueError(f"Unsupported protocol version: {protocol_version}")
        return serializer

    @classmethod
    def dumps(cls, data: MessageEntity, key: str, compress: bool, protocol_version: int = DEFAULT_PROTOCOL_VERSION) -> bytes:
        """
        序列化消息

        Args:
            data: 消息实体
            key: 加密密钥
            compress: 是否压缩
            protocol_version: 协议版本 (1=二进制, 2=msgpack)

        Returns:
            序列化后的字节数据
        """
        serializer = cls.get_serializer(protocol_version)
        return serializer.dumps(data, key, compress)

    @classmethod
    def loads(cls, byte_data: bytes, key: str, compress: bool, protocol_version: int = DEFAULT_PROTOCOL_VERSION) -> MessageEntity:
        """
        反序列化消息

        Args:
            byte_data: 字节数据
            key: 加密密钥
            compress: 是否解压
            protocol_version: 协议版本 (1=二进制, 2=msgpack)

        Returns:
            消息实体
        """
        serializer = cls.get_serializer(protocol_version)
        return serializer.loads(byte_data, key, compress)

    # 保持向后兼容的方法
    @classmethod
    def check_signature(cls, clear_text: bytes, data_len: int, key: str) -> bool:
        """检查签名（使用 V1 实现）"""
        return NatSerializationV1.check_signature(clear_text, data_len, key)

    @classmethod
    def check_nonce_and_timestamp(cls, clear_text: bytes) -> bool:
        """检查 nonce 和时间戳（使用 V1 实现）"""
        return NatSerializationV1.check_nonce_and_timestamp(clear_text)


# 保持向后兼容的常量
UID_LEN = 4
HEADER_LEN = 22


if __name__ == '__main__':
    ContextUtils.set_nonce_to_time({})

    def _print_commend(msg):
        print(''.join("b'{}'".format(''.join('\\x{:02x}'.format(b) for b in msg))))

    # 测试数据
    data = {
        'type_': 'a',
        'data': {
            'name': 'ssh',
            'uid': os.urandom(4),
            'ip_port': '127.0.0.1:8888',
            'data': b'test data',
            'source_client': 'client1',  # V2 支持的额外字段
            'speed_limit': 1024.0,       # V2 支持的额外字段
        }
    }
    key32 = 'xxxx'

    print("=== V1 测试 (二进制格式) ===")
    try:
        a1 = NatSerialization.dumps(data, key32, False, protocol_version=1)
        print(f"V1 序列化大小: {len(a1)} bytes")
        b1 = NatSerialization.loads(a1, key32, False, protocol_version=1)
        print(f"V1 反序列化: {b1}")
        print(f"注意: V1 不支持 source_client 和 speed_limit 字段")
    except Exception as e:
        print(f"V1 错误: {e}")

    print("\n=== V2 测试 (msgpack 格式) ===")
    a2 = NatSerialization.dumps(data, key32, False, protocol_version=2)
    print(f"V2 序列化大小: {len(a2)} bytes")
    b2 = NatSerialization.loads(a2, key32, False, protocol_version=2)
    print(f"V2 反序列化: {b2}")
    print(f"V2 支持所有字段，包括 source_client={b2['data'].get('source_client')}, speed_limit={b2['data'].get('speed_limit')}")
