import base64
from Crypto.Util.Padding import pad, unpad
from Crypto.Util.py3compat import bchr, bord
import Crypto.Cipher.AES as AES
import hashlib

BS = AES.block_size


class AES_Crypt:
    PADDING_PKCS5 = "PKCS5"
    PADDING_PKCS7 = "PKCS7"
    PADDING_ZERO = "ZEROPKCS"
    NO_PADDING = "NOPKCS"

    def __init__(self, key: bytes, mode: str = AES.MODE_ECB, padding: str = "NOPKCS") -> None:
        """AES crypt
        Encrypt fllow:
        key --> sha1prng encode|padding|customencode
        content --> padding --> transfer to bytes --> encrypt(ECB/CBC/...) --> transfer to format(base64/hexstr)
        Decrypt fllow:
        key --> sha1prng encode|padding|customencode
        encrypted(base64/hexstr) --> transfer to bytes --> decrypt(ECB/CBC/...) --> unpadding --> transfer to str
        codding by max.bai 2020-11
        Args:
            key (bytes): encrypt key, if mode is CBC, the key must be 16X len.
            mode (str, optional): AES mode. Defaults to AES.MODE_ECB.
            padding (str, optional): AES padding mode. Defaults to "NOPKCS".
        """
        self.key = key
        self.pkcs = padding

    @staticmethod
    def get_sha1prng_key(key: str) -> bytes:
        """
        encrypt key with SHA1PRNG
        same as java AES crypto key generator SHA1PRNG
        """
        signature = hashlib.sha1(key.encode()).digest()
        signature = hashlib.sha1(signature).digest()
        return signature[:16]

    @staticmethod
    def padding_pkcs5(value: str) -> bytes:
        """padding pkcs5 mode
        Args:
            value (str): need padding data
        Returns:
            bytes: after padding data with bytes type
        """
        padding_len = BS - len(value.encode()) % BS
        return value.encode() + bchr(padding_len) * padding_len

    @staticmethod
    def padding_zero(value: str) -> bytes:
        """padding with zero
        Args:
            value (str): need padding data
        Returns:
            bytes: after padding data with zero with bytes type
        """
        while len(value) % 16 != 0:
            value += "\0"
        return str.encode(value)

    @staticmethod
    def unpadding_pkcs5(value: bytes) -> bytes:
        """unpadding pkcs5 mode
        Args:
            value (bytes): need unpadding data
        Returns:
            bytes: after unpadding
        """
        padding_len = bord(value[-1])
        return value[:-padding_len]

    @staticmethod
    def unpadding_zero(value: bytes) -> bytes:
        """unpadding zero mode
        Args:
            value (bytes): need unpadding data
        Returns:
            bytes: after unpadding
        """
        return value

    @staticmethod
    def bytes_to_base64(value: bytes) -> str:
        """
        bytes transfer to base64 format
        """
        return base64.b64encode(value).decode()

    @staticmethod
    def base64_to_bytes(value: str) -> bytes:
        """
        base64 transfer to bytes
        """
        return base64.b64decode(value)

    @staticmethod
    def bytes_to_hex(value: bytes) -> str:
        """
        bytes transfer to hex str format
        """
        return value.hex().upper()

    def _get_padding_value(self, content: str) -> bytes:
        """get padding value from padding data
        Only add pkcs5, pkcs7, zero, nopadding mode,
        you can add your padding mode and unpadding mode in this
        section
        Args:
            content (str): need padding data
        Raises:
            Exception: no supporting padding mode
        Returns:
            bytes: padded data
        """
        if self.pkcs == AES_Crypt.PADDING_PKCS5:
            padding_value = self.padding_pkcs5(content)
        elif self.pkcs == AES_Crypt.PADDING_PKCS7:
            padding_value = pad(content.encode(), BS, style="pkcs7")
        elif self.pkcs == AES_Crypt.PADDING_ZERO:
            padding_value = self.padding_zero(content)
        elif self.pkcs == AES_Crypt.NO_PADDING:
            padding_value = str.encode(content)
        else:
            raise Exception("No supporting padding mode! Not implation padding mode!")
        return padding_value

    def _get_unpadding_value(self, content: bytes) -> bytes:
        """get unpadding value from padded data
        Only add pkcs5, pkcs7, zero, nopadding mode,
        you can add your padding mode and unpadding mode in this
        section
        Args:
            content (str): need unpadding data
        Raises:
            Exception: no supporting padding mode
        Returns:
            bytes: unpadded data
        """
        if self.pkcs == AES_Crypt.PADDING_PKCS5:
            unpadding_value = self.unpadding_pkcs5(content)
        elif self.pkcs == AES_Crypt.PADDING_PKCS7:
            unpadding_value = unpad(content, BS, style="pkcs7")
        elif self.pkcs == AES_Crypt.PADDING_ZERO:
            unpadding_value = self.unpadding_zero(content)
        elif self.pkcs == AES_Crypt.NO_PADDING:
            unpadding_value = content
        else:
            raise Exception("No supporting padding mode! Not implation padding mode!")

        return unpadding_value

    # ECB encrypt
    def ECB_encrypt_to_bytes(self, content: str) -> bytes:
        """ECB encrypt to bytes type
        Args:
            content (str): need encrypt content
        Returns:
            bytes: encrypted content with bytes type
        """
        cryptor = AES.new(self.key, AES.MODE_ECB)

        padding_value = self._get_padding_value(content)

        ciphertext = cryptor.encrypt(padding_value)
        return ciphertext

    def ECB_encrypt_to_base64(self, content: str) -> str:
        """ECB encrypt to base64 format
        Args:
            content (str): need encrypt content
        Returns:
            str: encrypted content with base64 format
        """
        ciphertext = self.ECB_encrypt_to_bytes(content)
        return self.bytes_to_base64(ciphertext)

    def ECB_encrypt_to_hex(self, content: str) -> str:
        """ECB encrypt to hex str format
        Args:
            content (str): need encrypt content
        Returns:
            str: encrypted content with hex str format
        """
        ciphertext = self.ECB_encrypt_to_bytes(content)
        return self.bytes_to_hex(ciphertext)

    # ECB decrypt
    def ECB_decrypt_from_bytes(self, ciphertext: bytes) -> bytes:
        """ECB decrypt from bytes type
        Args:
            ciphertext (bytes): need decrypt data
        Returns:
            bytes: decrypted content with bytes type
        """
        cryptor = AES.new(self.key, AES.MODE_ECB)
        content = cryptor.decrypt(ciphertext)

        unpadding_value = self._get_unpadding_value(content)
        return unpadding_value

    def ECB_decrypt_from_base64(self, ciphertext: str) -> str:
        """ECB decrypt from base64 format
        Args:
            ciphertext (str): need decrypt data
        Returns:
            str: decrypted content
        """
        ciphertext_bytes = self.base64_to_bytes(ciphertext)
        content = self.ECB_decrypt_from_bytes(ciphertext_bytes)
        return content.decode()

    def ECB_decrypt_from_hex(self, ciphertext: str) -> str:
        """ECB decrypt from hex str format
        Args:
            ciphertext (str): need decrypt data
        Returns:
            str: decrypted content
        """
        content = self.ECB_decrypt_from_bytes(bytes.fromhex(ciphertext))
        return content.decode()

    # CBC encrypt
    def CBC_encrypt_to_bytes(self, content: str, iv: str) -> bytes:
        """CBC encrypt to bytes type
        Args:
            content (str): need encrypt content, must 16x length
            iv (str): iv, need 16X len
        Returns:
            bytes: encrypted data
        """
        cryptor = AES.new(self.key, AES.MODE_CBC, iv=iv.encode())

        padding_value = self._get_padding_value(content)

        ciphertext = cryptor.encrypt(padding_value)
        return ciphertext

    def CBC_encrypt_to_base64(self, content: str, iv: str) -> str:
        """CBC encrypt to base64 format
        Args:
            content (str): need encrypt content, must 16x length
            iv (str): iv, need 16X len
        Returns:
            str: encrypted data with base64 format
        """
        ciphertext = self.CBC_encrypt_to_bytes(content, iv)
        return self.bytes_to_base64(ciphertext)

    def CBC_encrypt_to_hex(self, content: str, iv: str) -> str:
        """CBC encrypt to hex str format
        Args:
            content (str): need encrypt content, must 16x length
            iv (str): iv, need 16X len
        Returns:
            str: encrypted data with hex str format
        """
        ciphertext = self.CBC_encrypt_to_bytes(content, iv)
        return self.bytes_to_hex(ciphertext)

    # CBC decrypt
    def CBC_decrypt_from_bytes(self, ciphertext: bytes, iv: str) -> bytes:
        """ECB decrypt from bytes type
        Args:
            ciphertext (bytes): need decrypt data
            iv (str): iv, need 16X len
        Returns:
            bytes: decrypted content with bytes type
        """
        cryptor = AES.new(self.key, AES.MODE_CBC, iv=iv.encode())
        content = cryptor.decrypt(ciphertext)

        unpadding_value = self._get_unpadding_value(content)
        return unpadding_value

    def CBC_decrypt_from_base64(self, ciphertext: str, iv: str) -> str:
        """ECB decrypt from base64 format
        Args:
            ciphertext (str): need decrypt data
            iv (str): iv, need 16X len
        Returns:
            str: decrypted content
        """
        ciphertext_bytes = self.base64_to_bytes(ciphertext)
        content = self.CBC_decrypt_from_bytes(ciphertext_bytes, iv)
        return content.decode()

    def CBC_decrypt_from_hex(self, ciphertext: str, iv: str) -> str:
        """ECB decrypt from hex str format
        Args:
            ciphertext (str): need decrypt data
            iv (str): iv, need 16X len
        Returns:
            str: decrypted content
        """
        content = self.CBC_decrypt_from_bytes(bytes.fromhex(ciphertext), iv)
        return content.decode()

