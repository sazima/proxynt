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


if __name__ == '__main__':
    KEY = 'hello world'
    res = EncryptUtils.encode(b'0x010x02', key=KEY)
    print(res)
    KEY = 'hello worla'
    res = EncryptUtils.decode(res, KEY)
    print(res)
