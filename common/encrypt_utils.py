from itertools import cycle
from random import randint


class EncryptUtils:

    @classmethod
    def encode(cls, cleartext: bytes, key: str):
        return cleartext  # todo: 没有加密
        reps = (len(cleartext) - 1) // len(key) + 1
        # a1 = cleartext.encode('utf-8')
        key = (key * reps)[:len(cleartext)].encode('utf-8')
        cipher = bytes([i1 ^ i2 for (i1, i2) in zip(cleartext, key)])
        return cipher

    @classmethod
    def decode(cls, cipher: bytes, key) -> bytes:
        return cipher
        reps = (len(cipher) - 1) // len(key) + 1
        key = (key * reps)[:len(cipher)].encode('utf-8')
        clear = bytes([i1 ^ i2 for (i1, i2) in zip(cipher, key)])
        return clear


    # @classmethod
    # def encrypt_dummy1(cls, text, password):
    #     addition_char = randint(0, 0x100)
    #     if len(text) > len(password):
    #         pwd_iterable = cycle(password)
    #     else:
    #         pwd_iterable = password
    #     ret = [chr(((ord(i) ^ ord(j)) + addition_char) % 0x100) for i, j in izip(text, pwd_iterable)]
    #     return "".join(reversed(ret)) + chr(addition_char)
    #
    #
    # @classmethod
    # def decrypt_dummy1(cls, encrypted_text, password):
    #     addition_char = ord(encrypted_text[-1])
    #     if len(encrypted_text) > len(password):
    #         pwd_iterable = cycle(password)
    #     else:
    #         pwd_iterable = password
    #     ret = [chr((((ord(i) - addition_char) + 0x100) % 0x100) ^ ord(j)) for i, j in izip(reversed(encrypted_text[:-1]), pwd_iterable)]
    #     return "".join(ret)

# -*- coding: utf-8 -*-
'''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''
#作者：cacho_37967865
#博客：https://blog.csdn.net/sinat_37967865
#文件：encryption.py
#日期：2019-07-31
#备注：多种加解密方法    # pip install pycryptodome
用pyCryptodome模块带的aes先将秘钥以及要加密的文本填充为16位   AES key must be either 16, 24, or 32 bytes long
'''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''''
import base64
from Crypto.Cipher import AES


#  bytes不是32的倍数那就补足为32的倍数
def add_to_32(value):
    while len(value) % 32 != 0:
        value += b'\x00'
    return value     # 返回bytes


# str转换为bytes超过32位时处理
def cut_value(org_str):
    org_bytes = str.encode(org_str)
    n = int(len(org_bytes) / 32)
    print('bytes长度：',len(org_bytes))
    i = 0
    new_bytes = b''
    while n >= 1:
        i = i + 1
        new_byte = org_bytes[(i-1)*32:32*i-1]
        new_bytes += new_byte
        n = n - 1
    if len(org_bytes) % 32 == 0:                   # 如果是32的倍数，直接取值
        all_bytes = org_bytes
    elif len(org_bytes) % 32 != 0 and n>1:         # 如果不是32的倍数，每次截取32位相加，最后再加剩下的并补齐32位
        all_bytes = new_bytes + add_to_32 (org_bytes[i*32:])
    else:
        all_bytes = add_to_32 (org_bytes)          # 如果不是32的倍数，并且小于32位直接补齐
    print(all_bytes)
    return all_bytes


def AES_encrypt(org_str,key):
    # 初始化加密器
    aes = AES.new(cut_value(key), AES.MODE_ECB)
    #先进行aes加密
    encrypt_aes = aes.encrypt(cut_value(org_str))
    # 用base64转成字符串形式
    encrypted_text = str(base64.encodebytes(encrypt_aes), encoding='utf-8')  # 执行加密并转码返回bytes
    print(encrypted_text)
    return(encrypted_text)


def AES_decrypt(secret_str,key):
    # 初始化加密器
    aes = AES.new(cut_value(key), AES.MODE_ECB)
    # 优先逆向解密base64成bytes
    base64_decrypted = base64.decodebytes(secret_str.encode(encoding='utf-8'))
    # 执行解密密并转码返回str
    decrypted_text = str(aes.decrypt(base64_decrypted), encoding='utf-8').replace('\0', '')
    print(decrypted_text)


if __name__ == '__main__':
    org_str = 'http://mp.weixin.qq.com/s?__biz=MjM5NjAxOTU4MA==&amp;mid=3009217590&amp;idx=1&amp;sn=14532c49bc8cb0817544181a10e9309f&amp;chksm=90460825a7318133e7905c02e708d5222abfea930e61b4216f15b7504e39734bcd41cfb0a26d&amp;scene=27#wechat_redirect'
    # 秘钥
    key = '123abc'
    secret_str = AES_encrypt(org_str,key)
    AES_decrypt(secret_str,key)
# if __name__ == '__main__':
#     KEY = 'hello world'
#     res = EncryptUtils.encode(b'0x010x02', key=KEY)
#     print(res)
#     KEY = 'hello worla'
#     res = EncryptUtils.decode(res, KEY)
#     print(res)
