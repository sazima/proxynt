import json
import logging
import sys
from optparse import OptionParser

import tornado.ioloop
import tornado.web
from tornado_request_mapping import Route

from common.logger_factory import LoggerFactory
from context.context_utils import ContextUtils
from entity.server_config_entity import ServerConfigEntity
from server.websocket_handler import MyWebSocketaHandler

DEFAULT_CONFIG = './config_s.json'
DEFAULT_LOGGER_LEVEL = logging.INFO

name_to_level = {
    'debug': logging.DEBUG,
    'info': logging.INFO,
    'warn': logging.WARN,
    'error': logging.ERROR
}


def load_config():
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
    LoggerFactory.get_logger().info(f'use config path : {config_path}')
    config_path = config_path or DEFAULT_CONFIG
    with open(config_path, 'r') as rf:
        content_str = rf.read()
    content_json: ServerConfigEntity = json.loads(content_str)
    password = content_json.get('password', '')
    port = content_json.get('port', )
    if not port:
        raise Exception('server port is required')
    ContextUtils.set_password(password)
    ContextUtils.set_port(int(port))


if __name__ == "__main__":
    load_config()
    app = tornado.web.Application()
    route = Route(app)
    route.register(MyWebSocketaHandler)
    app.listen(ContextUtils.get_port())
    LoggerFactory.get_logger().info(f'start server at port {ContextUtils.get_port()}..')
    tornado.ioloop.IOLoop.current().start()
