class SystemConstant:
    CHUNK_SIZE = 65536 * 1
    DEFAULT_TIMEOUT = 0.5

    HEART_BEAT_INTERVAL = 15
    MAX_HEART_BEAT_SECONDS = 60  # 超过一定秒数没有心跳就关闭
    MAX_TIME_DIFFERENCE = 360  # 6分钟

    ADMIN_PATH = 'admin'
    GENERATOR_NAME_PREFIX = 'admin_'

    ENCRYPT_METHOD = 'table'

    PACKAGE_NAME = 'proxynt'

    COOKIE_EXPIRE_SECONDS = 3600 * 24

    VERSION = '2.0.44'

    GITHUB = 'https://github.com/sazima/proxynt'

    # P2P settings
    P2P_SUPPORTED_VERSION = '2.0.3'  # Minimum version that supports P2P
    P2P_HOLE_PUNCH_TIMEOUT = 5       # Seconds to wait for hole punching
    P2P_MAX_RETRY = 3                # Max retry attempts for hole punching
