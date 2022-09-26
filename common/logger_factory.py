import datetime
import os
import logging
from logging.handlers import TimedRotatingFileHandler

from pytz import timezone


class LoggerFactory:
    fmt = " %(asctime)s %(filename)s %(lineno)s %(funcName)s %(message)s"
    logger = logging.getLogger("loger")
    tz = 'Asia/Shanghai'

    @classmethod
    def get_logger(cls):
        if hasattr(cls, '_log'):
            return cls.logger
        cls.logger.setLevel(logging.DEBUG)
        cls._add_file_handler(cls.logger)
        cls._add_console_handler(cls.logger)
        cls._log = cls.logger
        logging.Formatter.converter = lambda *args: datetime.datetime.now(tz=timezone(cls.tz)).timetuple()
        return cls.logger

    @classmethod
    def _add_console_handler(cls, logger):
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(cls.fmt))
        logger.addHandler(handler)

    @classmethod
    def _add_file_handler(cls, logger):
        os.makedirs('log', exist_ok=True)
        handler = TimedRotatingFileHandler(os.path.join('log', 'log.log'), when="d")
        formatter = logging.Formatter(cls.fmt)
        handler.setFormatter(formatter)
        logger.addHandler(handler)


