# 如果包装不了 就按照下面2个步骤
from Crypto.Util.Padding import pad
from Crypto.Cipher import AES

class PrpCrypt(object):
    @classmethod
    def format_to_32(cls, k):
        key32 = "{: <16}".format(k)
        return key32

    @classmethod
    def encrypt(cls,  aes_bytes: bytes, key: str):
        key = cls.format_to_32(key)
        # 使用key,选择加密方式
        aes = AES.new(key.encode('utf-8'), AES.MODE_ECB)
        pad_pkcs7 = pad(aes_bytes, AES.block_size, style='pkcs7')  # 选择pkcs7补全
        encrypt_aes = aes.encrypt(pad_pkcs7)
        return encrypt_aes

    @classmethod
    def decrypt(cls,  decrData: bytes, key: str) -> bytes:  # 解密函数
        key = cls.format_to_32(key)
        # res = base64.decodebytes(decrData)
        aes = AES.new(key.encode('utf-8'), AES.MODE_ECB)
        msg = aes.decrypt(decrData)
        return msg


if __name__ == '__main__':
    # key的长度需要补长(16倍数),补全方式根据情况而定,此处我就手动以‘0’的方式补全的32位key
    # key字符长度决定加密结果,长度16：加密结果AES(128),长度32：结果就是AES(256)
    key = "c72e5b90d42e406e907ceaecc7b00234"
    # # 加密字符串长同样需要16倍数：需注意,不过代码中pad()方法里，帮助实现了补全（补全方式就是pkcs7）

