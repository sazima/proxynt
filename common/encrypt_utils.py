from common.crypto.table import TableCipher
import hashlib

try:
    import xxhash
    _XXHASH_AVAILABLE = True
except ImportError:
    _XXHASH_AVAILABLE = False

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

    @classmethod
    def xxhash64_hash(cls, data: bytes) -> bytes:
        """
        使用 xxHash64 生成 8 字节签名（比 MD5 快 10 倍）
        需要安装: pip install xxhash
        """
        if not _XXHASH_AVAILABLE:
            raise ImportError('xxhash is not installed, run: pip install xxhash')
        result = xxhash.xxh64(data)
        return result.digest()  # 8 字节

    @classmethod
    def is_xxhash_available(cls) -> bool:
        """检查 xxHash 是否可用"""
        return _XXHASH_AVAILABLE



if __name__ == '__main__':
    KEY = 'hello world'
    KEY = KEY.rjust(16)
    res = EncryptUtils.encrypt(b'xa' * 2 , key=KEY)
    print(res)
    res = EncryptUtils.decrypt(res, KEY)
    print(res)
