class SystemConstant:
    CHUNK_SIZE = 65536 * 8
    DEFAULT_TIMEOUT = 0.5

    HEART_BEAT_INTERVAL = 10
    MAX_TIME_DIFFERENCE = 360  # 十分钟

    MAX_HEART_BEAT_SECONDS = 60  # 超过一定秒数没有心跳就关闭
    ADMIN_PATH = 'admin'
    GENERATOR_NAME_PREFIX = 'admin_'

    ENCRYPT_METHOD = 'table'

    PACKAGE_NAME = 'proxynt'

    COOKIE_EXPIRE_SECONDS = 3600 * 24

    VERSION = '1.1.4'

    GITHUB = 'https://github.com/sazima/proxynt'
