import json
import signal
import sys
import time
from threading import Thread
from typing import List, Dict, Tuple, Set

import rel as rel
import websocket

from client.tcp_forward_client import TcpForwardClient
from common.logger_factory import LoggerFactory
from constant.message_type_constnat import MessageTypeConstant
from entity.client_config_entity import ClientConfigEntity
from entity.message.message_entity import MessageEntity
from entity.message.push_config_entity import PushConfigEntity
from entity.message.tcp_over_websocket_message import TcpOverWebsocketMessage
from exceptions.duplicated_name import DuplicatedName

import rel

name_to_addr: Dict[str, Tuple[str, int]] = dict()
with open('./config_c.json', 'r') as rf:
    config_data: ClientConfigEntity = json.loads(rf.read())
name_set: Set[str] = set()

for client in config_data['client']:
    if client['name'] in name_set:
        raise DuplicatedName()
    name_set.add(client['name'])
    addr = (client['local_ip'], client['local_port'])
    name_to_addr[client['name']] = addr


def on_message(ws, message: str):
    message_data: MessageEntity = json.loads(message)
    if message_data['type_'] == MessageTypeConstant.WEBSOCKET_OVER_TCP:
        # LoggerFactory.get_logger().debug(f'get websocket message {message_data}')
        data: TcpOverWebsocketMessage = message_data['data']
        uid = data['uid']
        name = data['name']
        b = bytes.fromhex(data['data'])
        forward_client.create_socket(name, uid)
        forward_client.send_by_uid(uid, b)


# def on_error(ws, error):
#     print('error ------------- ')
#     if isinstance(error, KeyboardInterrupt):
#         sys.exit(0)


def on_close(ws, a, b):
    LoggerFactory.get_logger().info(f'close {ws}, {a}, {b} ')
    forward_client.close()


def on_open(ws):
    push_configs: List[PushConfigEntity] = config_data['client']
    message: MessageEntity = {
        'type_': MessageTypeConstant.PUSH_CONFIG,
        'data': push_configs
    }
    forward_client.is_running = True
    ws.send(json.dumps(message))
    task = Thread(target=forward_client.start_forward)
    task.start()


def signal_handler(sig, frame):
    print('You pressed Ctrl+C!')
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)

if __name__ == "__main__":
    server_config = config_data['server']
    url = ''
    if server_config['https']:
        url += 'wss://'
    else:
        url += 'ws://'
    url += server_config['host'] + ":" + str(server_config['port']) + "/ws?password=" + server_config['password']
    ws = websocket.WebSocketApp(url,
                                on_message=on_message,
                                on_close=on_close)
    ws.on_open = on_open
    forward_client = TcpForwardClient(name_to_addr, ws)
    LoggerFactory.get_logger().info('start success')
    while True:
        try:
            ws.run_forever()  # Set dispatcher to automatic reconnection
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            pass
        LoggerFactory.get_logger().info(f'try after 2 seconds')
        time.sleep(2)
    # rel.signal(2, rel.abort)  # Keyboard Interrupt
    # rel.dispatch()
