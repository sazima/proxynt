from common.cryptor import Cryptor
from constant.system_constant import SystemConstant


class EncryptUtils:

    @classmethod
    def encrypt(cls, cleartext: bytes, key: str):
        c = Cryptor(key.encode(), SystemConstant.ENCRYPT_METHOD)
        return c.encrypt(cleartext)

    @classmethod
    def decrypt(cls, cipher: bytes, key) -> bytes:
        c = Cryptor(key.encode(), SystemConstant.ENCRYPT_METHOD)
        return c.decrypt(cipher)



if __name__ == '__main__':
    KEY = 'hello world'
    res = EncryptUtils.encrypt(b'0x010x02', key=KEY)
    print(res)
    res = EncryptUtils.decrypt(res, KEY)
    print(res)
