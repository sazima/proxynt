import json

import tornado.ioloop
import tornado.web
from tornado_request_mapping import Route

from context.context_utils import ContextUtils
from entity.server_config_entity import ServerConfigEntity
from server.websocket_handler import MyWebSocketaHandler


def load_config():
    with open('./config_s.json', 'r') as rf:
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
    tornado.ioloop.IOLoop.current().start()
