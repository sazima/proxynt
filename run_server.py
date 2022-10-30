import json
import logging
import sys
from optparse import OptionParser

import tornado.ioloop
import tornado.web

from common.logger_factory import LoggerFactory
from context.context_utils import ContextUtils
from entity.server_config_entity import ServerConfigEntity
from server.heart_beat_task import HeartBeatTask
from server.websocket_handler import MyWebSocketaHandler

DEFAULT_CONFIG = './config_s.json'
DEFAULT_LOGGER_LEVEL = logging.INFO
DEFAULT_WEBSOCKET_PATH = '/ws'

name_to_level = {
    'debug': logging.DEBUG,
    'info': logging.INFO,
    'warn': logging.WARN,
    'error': logging.ERROR
}


def load_config() -> ServerConfigEntity:
    parser = OptionParser(usage="usage: %prog -c config_s.json ")
    parser.add_option("-c", "--config",
                      type='str',
                      dest='config',
                      default=DEFAULT_CONFIG,
                      help="config json file"
                      )
    parser.add_option("-l", "--level",
                      type='str',
                      dest='log_level',
                      default='info',
                      help="log level: debug, info, warn , error"
                      )
    (options, args) = parser.parse_args()
    config_path = options.config
    log_level = options.log_level
    if log_level not in name_to_level:
        print('invalid log level.')
        sys.exit()
    ContextUtils.set_log_level(name_to_level[log_level])
    print(f'use config path : {config_path}')
    config_path = config_path or DEFAULT_CONFIG
    with open(config_path, 'r') as rf:
        content_str = rf.read()
    content_json: ServerConfigEntity = json.loads(content_str)
    password = content_json.get('password', '')
    port = content_json.get('port', )
    path = content_json.get('path', DEFAULT_WEBSOCKET_PATH)
    if not path.startswith('/'):
        print('path should startswith "/" ')
        sys.exit()
    if not port:
        raise Exception('server port is required')
    ContextUtils.set_password(password)
    ContextUtils.set_websocket_path(path)
    ContextUtils.set_port(int(port))
    ContextUtils.set_log_file(content_json.get('log_file'))


if __name__ == "__main__":
    heart_beat_task = HeartBeatTask()
    server_config = load_config()
    app = tornado.web.Application([
        (ContextUtils.get_websocket_path(), MyWebSocketaHandler),
    ])
    app.listen(ContextUtils.get_port(), chunk_size=65536 * 2)
    LoggerFactory.get_logger().info(f'start server at port {ContextUtils.get_port()}..')
    tornado.ioloop.PeriodicCallback(heart_beat_task.run, 5 * 1000).start()
    tornado.ioloop.IOLoop.current().start()
