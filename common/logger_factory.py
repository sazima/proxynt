import os
import logging
import os
from logging.handlers import TimedRotatingFileHandler, RotatingFileHandler

from context.context_utils import ContextUtils


# from pytz import timezone


class LoggerFactory:
    fmt = '%(asctime)s  %(name)s %(levelname)s %(pathname)s %(funcName)s %(lineno)d : %(message)s'
    logger = logging.getLogger("logger")
    default_log_file = os.path.join('log', 'log.log')
    backupCount = 10
    MAX_BYTES = 1024 * 1024 * 5  # 5 MB
    # tz = 'Asia/Shanghai'

    @classmethod
    def get_logger(cls):
        if hasattr(cls, '_log'):
            return cls.logger
        cls.logger.setLevel(ContextUtils.get_log_level())
        cls._add_file_handler(cls.logger)
        cls._add_console_handler(cls.logger)
        cls._log = cls.logger
        # logging.Formatter.converter = lambda *args: datetime.datetime.now(tz=timezone(cls.tz)).timetuple()
        return cls.logger

    @classmethod
    def _add_console_handler(cls, logger):
        handler = logging.StreamHandler()
        cls.logger.setLevel(ContextUtils.get_log_level())
        handler.setFormatter(logging.Formatter(cls.fmt))
        logger.addHandler(handler)

    @classmethod
    def _add_file_handler(cls, logger):
        os.makedirs('log', exist_ok=True)
        if ContextUtils.get_log_file():
            if  cls.check_log_directory(ContextUtils.get_log_file()):
                log_file = ContextUtils.get_log_file()
            else:
                print(f'set log file to default: {os.path.abspath(cls.default_log_file)}')
                log_file = cls.default_log_file
        else:
            log_file = cls.default_log_file
        cls.check_log_directory(log_file)
        handler = RotatingFileHandler(log_file,  backupCount=cls.backupCount, maxBytes=cls.MAX_BYTES)
        formatter = logging.Formatter(cls.fmt)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    @classmethod
    def check_log_directory(cls, log_file: str):
        try:
            dir_ = os.path.dirname(log_file)
            os.makedirs(dir_, exist_ok=True)
            return True
        except OSError as e:
            print(f'check_log_directory: {log_file} get os error: {e}')
            return False



