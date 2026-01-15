# proxynt/common/cert_utils.py

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import datetime
import ssl
import tempfile
import os
import atexit
def generate_quic_ssl_context(role='server'):
    """
    生成用于 QUIC 的临时自签名证书和 SSL Context
    """
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )

    name = x509.Name([
        x509.NameAttribute(x509.NameOID.COMMON_NAME, u"proxynt-p2p")
    ])

    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1000)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(u"proxynt-p2p")]),
            critical=False,
        )
        .sign(key, hashes.SHA256(), default_backend())
    )

    # 将证书和私钥写入临时文件（aioquic/ssl 需要文件路径或对象）
    # 这里我们直接创建 SSLContext
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS)

    # 导出为 PEM 格式
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    # 加载到 SSLContext
    # 注意：在 Python 中直接从内存加载 cert/key 到 SSLContext 比较麻烦，
    # 也就是为什么通常用临时文件。为了简单，这里我们用临时文件。

    # 写入临时文件，不自动删除
    f_cert = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    f_cert.write(cert_pem)
    f_cert.close()

    f_key = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    f_key.write(key_pem)
    f_key.close()

    # 注册退出时清理
    def cleanup():
        try: os.unlink(f_cert.name)
        except: pass
        try: os.unlink(f_key.name)
        except: pass

    atexit.register(cleanup)

    return f_cert.name, f_key.name