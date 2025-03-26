# 使用Python创建一个简单的UDP客户端
import socket
import time

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(1)
server_address = ('127.0.0.1', 35000)
message = '测试UDP转发'
while True:
    sock.sendto(message.encode(), server_address)
    time.sleep(3)
    print('start recv')
    data, server = sock.recvfrom(1024)
    print('recv', data)