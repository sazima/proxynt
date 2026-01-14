#!/usr/bin/env python3
"""
测试EXCHANGE协议
用于验证UDP EXCHANGE包能否正常发送和接收
"""
import socket
import sys
import time

def test_client(server_host, client_name):
    """测试发送EXCHANGE包"""
    print(f"Testing EXCHANGE from client: {client_name}")
    print(f"Server: {server_host}:19999")

    # 创建UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # 构造EXCHANGE包
    exchange_packet = b'EXCHANGE:' + client_name.encode('utf-8')

    # 发送3次
    for i in range(3):
        sock.sendto(exchange_packet, (server_host, 19999))
        print(f"  Sent EXCHANGE packet #{i+1}")
        time.sleep(0.1)

    # 等待ACK
    sock.settimeout(2.0)
    try:
        data, addr = sock.recvfrom(1024)
        print(f"  Received: {data} from {addr}")
    except socket.timeout:
        print("  No response (timeout)")

    sock.close()

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: python3 test_exchange.py <server_host> <client_name>")
        print("Example: python3 test_exchange.py jifen.mp-wexin.work test_client")
        sys.exit(1)

    server_host = sys.argv[1]
    client_name = sys.argv[2]

    test_client(server_host, client_name)
