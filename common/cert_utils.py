import datetime
import os
import tempfile
import atexit
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

def get_quic_cert_paths():
    """
    生成用于 QUIC 的临时自签名证书和私钥文件。
    返回: (cert_path, key_path)
    """
    # 1. 生成 RSA 私钥
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )

    # 2. 生成自签名证书
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, u"proxynt-p2p")
    ])

    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1000)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=10)) # 有效期10天
        .add_extension(
            # 这里的域名必须和上面 QuicConfiguration 里的 server_name 一致
            x509.SubjectAlternativeName([x509.DNSName(u"proxynt-p2p")]),
            critical=False,
        )
        .sign(key, hashes.SHA256(), default_backend())
    )

    # 3. 序列化为 PEM 格式
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )

    # 4. 写入临时文件 (aioquic 需要文件路径)
    # delete=False 确保文件在关闭后仍然存在，供 SSLContext 读取
    f_cert = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
    f_cert.write(cert_pem)
    f_cert.close()

    f_key = tempfile.NamedTemporaryFile(delete=False, suffix=".key")
    f_key.write(key_pem)
    f_key.close()

    # 5. 注册退出清理逻辑
    def cleanup():
        try:
            if os.path.exists(f_cert.name):
                os.unlink(f_cert.name)
            if os.path.exists(f_key.name):
                os.unlink(f_key.name)
        except Exception:
            pass

    atexit.register(cleanup)

    return f_cert.name, f_key.name