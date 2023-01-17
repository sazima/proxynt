from common.crypto.table import TableCipher

class EncryptUtils:

    @classmethod
    def encrypt(cls, cleartext: bytes, key: str):
        cipher = TableCipher(key.encode(), 1)
        return cipher.encrypt(cleartext)

    @classmethod
    def decrypt(cls, cipher: bytes, key) -> bytes:
        decipher = TableCipher(key.encode(), 0)
        return decipher.decrypt(cipher)



if __name__ == '__main__':
    KEY = 'hello world'
    res = EncryptUtils.encrypt(b'0x010x02', key=KEY)
    print(res)
    res = EncryptUtils.decrypt(res, KEY)
    print(res)
