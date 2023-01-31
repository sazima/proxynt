import base64
import json
import os
import time
import traceback
from typing import List, Set

from tornado.web import RequestHandler

from common.logger_factory import LoggerFactory
from constant.system_constant import SystemConstant
from context.context_utils import ContextUtils
from entity.message.push_config_entity import PushConfigEntity, ClientData
from entity.server_config_entity import ServerConfigEntity
from server.tcp_forward_client import TcpForwardClient
from server.websocket_handler import MyWebSocketaHandler

# todo: 身份认证
COOKIE_KEY = 'c'
MIN_PORT = 1000
NOT_LOGIN = 401


class AdminHtmlHandler(RequestHandler):
    async def get(self):
        result = self.get_cookie(COOKIE_KEY)
        cookie_dict = ContextUtils.get_cookie_to_time()
        if result in cookie_dict and time.time() - cookie_dict[result] < SystemConstant.COOKIE_EXPIRE_SECONDS:
            self.render('ele_index.html')
        else:
            self.render('login.html')

    async def post(self):
        try:
            body_data = json.loads(self.request.body)
            password = body_data['password']
            admin_config = ContextUtils.get_admin_config()
            if admin_config and admin_config['admin_password'] == password:
                cookie_value = base64.b64encode(os.urandom(64)).decode()
                cookie_dict = ContextUtils.get_cookie_to_time()
                cookie_dict[cookie_value] = time.time()
                self.set_cookie(COOKIE_KEY, cookie_value)
                self.write({
                    'code': 200,
                    'data': '',
                    'msg': ''
                })
            else:
                self.write({
                    'code': 400,
                    'data': '',
                    'msg': '密码错误'
                })
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())


class ShowVariableHandler(RequestHandler):
    def get(self):
        forward_client = TcpForwardClient.get_instance()
        dict_ = forward_client.__dict__
        self.write({
            str(k): {str(k1): str(v1) for k1, v1 in v.items()} if isinstance(v, dict)
            else str(v) for k, v in dict_.items()
        })


class AdminHttpApiHandler(RequestHandler):
    async def get(self):
        try:
            cookie_dict = ContextUtils.get_cookie_to_time()
            result = self.get_cookie(COOKIE_KEY)
            if result not in cookie_dict or time.time() - cookie_dict[result] >= SystemConstant.COOKIE_EXPIRE_SECONDS:
                self.write({
                    'code': NOT_LOGIN,
                    'data': '',
                    'msg': '登录信息已经过期, 请重新刷新页面'
                })
                return
            online_client_name_list: List[str] = list(MyWebSocketaHandler.client_name_to_handler.keys())
            return_list = []
            client_name_to_config_list_in_server = ContextUtils.get_client_name_to_config_in_server()
            online_set: Set[str] = set()
            for client_name in online_client_name_list:
                handler = MyWebSocketaHandler.client_name_to_handler.get(client_name)
                push_config: PushConfigEntity = handler.push_config
                if handler is None:
                    continue
                config_list = push_config['config_list']  # 转发配置列表
                name_in_server: List[str] = list()
                for x in client_name_to_config_list_in_server.get(client_name, []):
                    name_in_server.append(x['name'])
                return_list.append({
                    'client_name': client_name,
                    'config_list': config_list,
                    'status': 'online',
                    'can_delete_names': [x['name'] for x in client_name_to_config_list_in_server.get(client_name, [])]
                    # 配置在服务器上的, 可以删除
                })
                online_set.add(client_name)

            for client_name, config_list in client_name_to_config_list_in_server.items():
                if client_name in online_set:
                    continue
                return_list.append({
                    'client_name': client_name,
                    'config_list': config_list,
                    'status': 'offline',
                    'can_delete_names': [x['name'] for x in client_name_to_config_list_in_server.get(client_name, [])]
                })
            return_list.sort(key=lambda x: x['client_name'])
            self.write({
                'code': 200,
                'data': return_list,
                'msg': ''
            })
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def delete(self, *args, **kwargs):
        try:
            # request_data = json.loads(self.request.body)
            cookie_dict = ContextUtils.get_cookie_to_time()
            result = self.get_cookie(COOKIE_KEY)
            if result not in cookie_dict or time.time() - cookie_dict[result] >= SystemConstant.COOKIE_EXPIRE_SECONDS:
                self.write({
                    'code': NOT_LOGIN,
                    'data': '',
                    'msg': '登录信息已经过期, 请重新刷新页面'
                })
                return
            online_client_name_list: List[str] = list(MyWebSocketaHandler.client_name_to_handler.keys())
            client_name = self.get_argument('client_name')
            name = self.get_argument('name')
            LoggerFactory.get_logger().info(f'delete {client_name}, {name}')
            if not client_name or not name:
                self.write({
                    'code': 400,
                    'data': '',
                    'msg': 'client ,name 不能为空'
                })
                return
            client_to_server_config = ContextUtils.get_client_name_to_config_in_server()
            old_config = client_to_server_config[client_name]
            new_config = [x for x in old_config if x['name'] != name]
            if not new_config:
                client_to_server_config.pop(client_name)
            else:
                client_to_server_config[client_name] = new_config
            self.write({
                'code': 200,
                'data': '',
                'msg': ''
            })
            self.update_config_file()
            if client_name in MyWebSocketaHandler.client_name_to_handler:
                MyWebSocketaHandler.client_name_to_handler[client_name].close(0, 'close by server')
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    async def post(self):
        try:
            cookie_dict = ContextUtils.get_cookie_to_time()
            result = self.get_cookie(COOKIE_KEY)
            if result not in cookie_dict or time.time() - cookie_dict[result] >= SystemConstant.COOKIE_EXPIRE_SECONDS:
                self.write({
                    'code': NOT_LOGIN,
                    'data': '',
                    'msg': '登录信息已经过期, 请重新刷新页面'
                })
                return
            online_client_name_list: List[str] = list(MyWebSocketaHandler.client_name_to_handler.keys())
            request_data = json.loads(self.request.body)
            LoggerFactory.get_logger().info(f'add config {request_data}')
            client_name = request_data.get('client_name')
            name = request_data.get('name')
            remote_port = int(request_data.get('remote_port'))
            local_ip = request_data.get('local_ip')
            local_port = int(request_data.get('local_port'))
            if not client_name:
                self.write({
                    'code': 400,
                    'data': '',
                    'msg': 'client name 不能为空'
                })
                return
            if not remote_port:
                self.write({
                    'code': 400,
                    'data': '',
                    'msg': '远程ip不能为空'
                })
                return
            if not local_ip:
                self.write({
                    'code': 400,
                    'data': '',
                    'msg': '必填local_ip'
                })
                return
            if not local_port or (local_port <= 0 or local_port > 65535):
                self.write({
                    'code': 400,
                    'data': '',
                    'msg': '本地port不合法'
                })
                return
            if client_name in MyWebSocketaHandler.client_name_to_handler:
                handler = MyWebSocketaHandler.client_name_to_handler[client_name]
                names = handler.names
            else:
                names = set()
            if not name or name in names:
                self.write({
                    'code': 400,
                    'data': '',
                    'msg': 'name不合法或者重复'
                })
                return
            if self.is_port_in_use(remote_port):
                self.write({
                    'code': 400,
                    'data': '',
                    'msg': '远程端口已占用, 请更换端口'
                })
                return
            if remote_port < MIN_PORT:
                self.write({
                    'code': 400,
                    'data': '',
                    'msg': f'端口最小为 {MIN_PORT}, 请更换端口'
                })
                return

            new_config: ClientData = {
                'name': name,
                'remote_port': remote_port,
                'local_port': local_port,
                'local_ip': local_ip
            }

            client_name_to_config_in_server = ContextUtils.get_client_name_to_config_in_server()
            if client_name in client_name_to_config_in_server:
                for c in client_name_to_config_in_server[client_name]:
                    if c['name'] == name:
                        self.write({
                            'code': 400,
                            'data': '',
                            'msg': 'name不合法'
                        })
                        return
                client_name_to_config_in_server[client_name].append(new_config)  # 更新配置
            else:
                client_name_to_config_in_server[client_name] = [new_config]  # 更新配置
            if client_name in MyWebSocketaHandler.client_name_to_handler:
                MyWebSocketaHandler.client_name_to_handler[client_name].close(0, 'close by server')
            self.write({
                'code': 200,
                'data': '',
                'msg': '成功'
            })
            self.update_config_file()
            return
            # with open(config_file_path, 'rb') as rf:
            #     server_config_data: ServerConfigEntity = json.load(rf)
            # client_config = server_config_data.get('client_config')
            # if not client_config:
            #     pass
        except Exception:
            LoggerFactory.get_logger().error(traceback.format_exc())

    def update_config_file(self):
        with open(ContextUtils.get_config_file_path(), 'r') as rf:
            content = rf.read()
        server_config: ServerConfigEntity = json.loads(content)
        server_config['client_config'] = ContextUtils.get_client_name_to_config_in_server()
        with open(ContextUtils.get_config_file_path(), 'w') as wf:
            wf.write(json.dumps(server_config, ensure_ascii=False, indent=4))

    @staticmethod
    def is_port_in_use(port: int) -> bool:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('localhost', port)) == 0
