import time
import threading
import socket
import struct
from .abstract_tunnel import AbstractTunnel
from common.kcp import Kcp
from common.logger_factory import LoggerFactory

# 隧道内部控制协议常量
CMD_DATA = 0x00
CMD_SYN  = 0x01
CMD_ACK  = 0x02
CMD_PING = 0x03
CMD_PONG = 0x04

class KcpTunnelImpl(AbstractTunnel):
    """
    基于 KCP 的 P2P 隧道实现。
    特性：
    1. 握手成功后升级 QoS 为 0xB8。
    2. 应用层握手/心跳保活。
    3. 小 MTU 避免分片。
    """
    def __init__(self, *args, **kwargs):
        # 提取参数，避免传给父类报错
        self.is_server = kwargs.pop('is_server', False)
        super().__init__(*args, **kwargs)

        self.logger = LoggerFactory.get_logger()
        self.kcp = Kcp(12345)
        self.kcp.output = self._udp_output

        # [核心] 隧道建立，接管 Socket，立即升级 QoS
        self._upgrade_socket_qos()

        # [核心] 限制 MTU 防止被运营商丢弃 (保留1字节给头部)
        self.kcp.setmtu(1000)

        # KCP 极速模式参数
        self.kcp.nodelay(1, 10, 2, 1)
        self.kcp.wndsize(256, 256)
        self.kcp.rx_minrto = 10
        self.kcp.fastresend = 1

        self.lock = threading.Lock()
        self.running = True

        # 状态管理
        self.is_ready = False       # 握手成功标志
        self.last_rx_time = time.time()
        self.last_ping_time = 0
        self.HEARTBEAT_INTERVAL = 5
        self.TIMEOUT_SECONDS = 15

    def _upgrade_socket_qos(self):
        """
        将 Socket 升级为高优先级模式 (DSCP=EF, 0xB8)。
        仅在隧道传输数据时使用，用于绕过 QoS 限速。
        """
        try:
            # 0xB8 = Expedited Forwarding (VoIP)
            self.sock.setsockopt(socket.SOL_IP, socket.IP_TOS, 0xB8)
            self.logger.info(f"Tunnel Socket upgraded to QoS 0xB8 (High Priority) for {self.peer_name}")
        except Exception as e:
            # 部分系统可能需要 root 权限，忽略错误
            self.logger.debug(f"Failed to set QoS: {e}")

    def establish(self):
        """启动接收线程并开始握手"""
        threading.Thread(target=self._recv_loop, daemon=True).start()

        self.logger.info(f"Starting KCP Handshake with {self.peer_name}...")

        # 发送 SYN 并等待 ACK
        for i in range(10):
            if not self.running: return

            # 如果已经就绪（收到 SYN 或 ACK），通知上层
            if self.is_ready:
                self.logger.info(f"Handshake success with {self.peer_name}")
                # [修正] 传递 self 对象
                self.on_established(self)
                return

            self._send_control(CMD_SYN)
            time.sleep(0.5)

        self.logger.warning(f"Handshake TIMEOUT with {self.peer_name}")
        self.close()

    def _udp_output(self, buf):
        """KCP 底层发送回调"""
        try:
            self.sock.sendto(buf, self.addr)
        except OSError:
            pass

    def _recv_loop(self):
        """UDP 接收循环"""
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65536)
                with self.lock:
                    self.kcp.input(data)
            except Exception:
                break

    def update(self):
        """主循环调用：驱动 KCP 状态机"""
        if not self.running: return

        current_ms = int(time.time() * 1000)
        now = time.time()

        with self.lock:
            self.kcp.update(current_ms)

            # 读取数据
            while True:
                size = self.kcp.peeksize()
                if size < 0: break

                data = self.kcp.recv()
                if not data or len(data) < 1: break

                self.last_rx_time = now

                cmd = data[0]
                payload = data[1:]

                if cmd == CMD_DATA:
                    if self.is_ready and len(payload) >= 4:
                        self.on_data_received(payload[:4], payload[4:])

                elif cmd == CMD_SYN:
                    self.is_ready = True
                    self._send_control_raw(CMD_ACK) # 收到 SYN，回 ACK

                elif cmd == CMD_ACK:
                    self.is_ready = True # 收到 ACK，握手完成

                elif cmd == CMD_PING:
                    self._send_control_raw(CMD_PONG) # 收到 PING，回 PONG

            # 检查死链
            if self.is_ready:
                if now - self.last_rx_time > self.TIMEOUT_SECONDS:
                    self.logger.warning(f"KCP Tunnel TIMEOUT ({self.peer_name})")
                    threading.Thread(target=self.close).start()
                    return

                # 发送心跳
                if now - self.last_ping_time > self.HEARTBEAT_INTERVAL:
                    self._send_control_raw(CMD_PING)
                    self.last_ping_time = now

    def send(self, uid: bytes, data: bytes) -> bool:
        """发送业务数据"""
        if not self.is_ready: return False

        with self.lock:
            try:
                # 协议封装: [DATA] + [UID] + [BODY]
                packet = bytes([CMD_DATA]) + uid + data

                # [关键修正] KCP Python版 send 返回 None，不返回字节数
                # 只要没抛出异常，就是写入缓冲成功
                self.kcp.send(packet)
                return True
            except Exception as e:
                self.logger.error(f"KCP send error: {e}")
                return False

    def _send_control(self, cmd_byte):
        with self.lock:
            self._send_control_raw(cmd_byte)

    def _send_control_raw(self, cmd_byte):
        try:
            self.kcp.send(bytes([cmd_byte]))
        except Exception:
            pass

    def close(self):
        if not self.running: return
        self.running = False
        self.is_ready = False
        try: self.sock.close()
        except: pass

        self.logger.info(f"KCP Tunnel closed for {self.peer_name}")
        self.on_closed(self.peer_name)