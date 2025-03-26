# 使用Python创建一个简单的UDP回显服务器
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', 5000))
print('UDP服务器已启动在端口5000')
while True:
    data, addr = sock.recvfrom(1024)
    print(f'收到来自 {addr} 的数据: {data.decode()}')
    sock.sendto(f'回显: {data.decode()}'.encode(), addr)