from common.crypto.table import TableCipher
import hashlib

class EncryptUtils:

    @classmethod
    def encrypt(cls, cleartext: bytes, key: str):
        cipher = TableCipher(key.encode(), )
        return cipher.encrypt(cleartext)

    @classmethod
    def decrypt(cls, cipher: bytes, key) -> bytes:
        decipher = TableCipher(key.encode(), )
        return decipher.decrypt(cipher)

    @classmethod
    def md5_hash(cls, data: bytes) -> bytes:
        # function
        result = hashlib.md5(data)
        return result.digest()



if __name__ == '__main__':
    KEY = 'hello world'
    KEY = KEY.rjust(16)
    res = EncryptUtils.encrypt(b'xa' * 2 , key=KEY)
    print(res)
    res = EncryptUtils.decrypt(res, KEY)
    print(res)
