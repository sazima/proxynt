import asyncio
import json
import logging
import os.path
import signal
import sys
from optparse import OptionParser


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tornado.ioloop
import tornado.web

from server.task.clear_nonce_task import ClearNonceTask
from server.task.check_cookie_task import CheckCookieTask
from common.logger_factory import LoggerFactory
from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils
from entity.server_config_entity import ServerConfigEntity
from server.admin_http_handler import AdminHtmlHandler, AdminHttpApiHandler, ShowVariableHandler
from server.task.heart_beat_task import HeartBeatTask
from server.tcp_forward_client import TcpForwardClient
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
    parser = OptionParser(usage="""usage: %prog -c config_s.json
    
config_s.json example: 
{
    "port": 18888,
    "password": "helloworld",
    "path": "/websocket_path",
    "log_file": "/var/log/nt/nt.log",
    "admin": {
        "enable": true,
        "admin_password": "new_password"
    }
}
""", version=SystemConstant.VERSION)
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
    ContextUtils.set_config_file_path(os.path.abspath(config_path))
    content_json: ServerConfigEntity = json.loads(content_str)
    port = content_json.get('port', )
    path = content_json.get('path', DEFAULT_WEBSOCKET_PATH)
    if not path.startswith('/'):
        print('path should startswith "/" ')
        sys.exit()
    if not port:
        raise Exception('server port is required')
    return content_json


def signal_handler(sig, frame):
    print('You pressed Ctrl+C!')
    os._exit(0)


def main():
    print('github: ', SystemConstant.GITHUB)
    signal.signal(signal.SIGINT, signal_handler)
    server_config = load_config()
    ContextUtils.set_cookie_to_time({})
    ContextUtils.set_nonce_to_time({})
    ContextUtils.set_password(server_config['password'])
    ContextUtils.set_websocket_path(server_config['path'])
    ContextUtils.set_port(int(server_config['port']))
    ContextUtils.set_log_file(server_config.get('log_file'))
    ContextUtils.set_client_name_to_config_in_server(server_config.get('client_config') or {})
    admin_enable = server_config.get('admin', {}).get('enable', False)  # 是否启动管理后台
    admin_password = server_config.get('admin', {}).get('admin_password', server_config['password'])  # 管理后台密码
    ContextUtils.set_admin_config(server_config.get('admin'))
    websocket_path = ContextUtils.get_websocket_path()
    admin_html_path = websocket_path + ('' if websocket_path.endswith('/') else '/') + SystemConstant.ADMIN_PATH  # 管理网页路径
    admin_api_path = websocket_path + ('' if websocket_path.endswith('/') else '/') + SystemConstant.ADMIN_PATH + '/api'  # 管理api路径
    show_variable_path = websocket_path + ('' if websocket_path.endswith('/') else '/') + SystemConstant.ADMIN_PATH + '/show_variable'  # 管理api路径
    status_url_path = websocket_path + ('' if websocket_path.endswith('/') else '/') + SystemConstant.ADMIN_PATH + '/static'  # static
    static_path = os.path.join(os.path.dirname(__file__), 'server', 'template')
    template_path = os.path.join(os.path.dirname(__file__), 'server', 'template')
    # if not os.path.isdir(static_path) and  os.path.abspath(__file__).startswith(sys.prefix):
    #     static_path = os.path.join(sys.prefix, 'local', SystemConstant.PACKAGE_NAME, 'server', 'template')
    #     template_path = os.path.join(sys.prefix, 'local', SystemConstant.PACKAGE_NAME, 'server', 'template')
    LoggerFactory.get_logger().info(f'static_path: {static_path}')
    tcp_forward_client = TcpForwardClient.get_instance()
    handlers = [
        (websocket_path, MyWebSocketaHandler),
    ]
    if admin_enable:
        handlers.extend([
            (admin_html_path, AdminHtmlHandler),
            (admin_api_path, AdminHttpApiHandler),
            (show_variable_path, ShowVariableHandler)
        ])
    app = tornado.web.Application(handlers, static_path=static_path, static_url_prefix=status_url_path, template_path=template_path)
    app.listen(ContextUtils.get_port(), chunk_size=65536 * 2)
    LoggerFactory.get_logger().info(f'start server at port {ContextUtils.get_port()}, websocket_path: {websocket_path}, admin_path: {admin_html_path}')
    heart_beat_task = HeartBeatTask(asyncio.get_event_loop())
    # create interval task
    check_cookie_task = CheckCookieTask()
    tornado.ioloop.PeriodicCallback(heart_beat_task.run, SystemConstant.HEART_BEAT_INTERVAL * 1000).start()
    tornado.ioloop.PeriodicCallback(check_cookie_task.run, 3600 * 1000).start()
    clear_nonce_stak = ClearNonceTask()
    tornado.ioloop.PeriodicCallback(clear_nonce_stak.run, 1800 * 1000).start()
    tornado.ioloop.IOLoop.current().start()


if __name__ == '__main__':
    main()
