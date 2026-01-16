"""
N4 Protocol Definitions for P2P Hole Punching

Extracted from p2ptest/n4.py for use in the main project.
Provides binary packet encoding/decoding for UDP hole punching.
"""
import struct
import socket
from typing import Optional, Tuple


class N4Packet:
    """
    N4 Protocol Packet Format:
    [ command (1 byte) | reserved (1 byte) | data (6 bytes) ]
    Total: 8 bytes fixed size
    """

    SIZE = 8

    # Command types
    CMD_HELLO = 0x01    # client --TCP-> server (identification)
    CMD_READY = 0x02    # client <-TCP-- server (both clients connected)
    CMD_EXCHG = 0x03    # client --UDP-> server (NAT mapping)
    CMD_PINFO = 0x04    # client <-TCP-- server (peer info)
    CMD_PUNCH = 0x05    # client <-UDP-> client (hole punching)

    RESERVED = 0x00

    @staticmethod
    def hello(ident: bytes) -> bytes:
        """Create HELLO packet for client identification"""
        pkt = struct.pack(
            "!BB6s", N4Packet.CMD_HELLO, N4Packet.RESERVED, ident[:6].ljust(6, b'\x00')
        )
        return pkt

    @staticmethod
    def dec_hello(pkt: bytes) -> Optional[bytes]:
        """Decode HELLO packet, returns ident or None"""
        if len(pkt) != N4Packet.SIZE:
            return None
        cmd, _, ident = struct.unpack("!BB6s", pkt)
        if cmd != N4Packet.CMD_HELLO:
            return None
        return ident

    @staticmethod
    def ready() -> bytes:
        """Create READY packet to signal both clients connected"""
        pkt = struct.pack(
            "!BB6s", N4Packet.CMD_READY, N4Packet.RESERVED, b'\x00' * 6
        )
        return pkt

    @staticmethod
    def dec_ready(pkt: bytes) -> Optional[bool]:
        """Decode READY packet, returns True or None"""
        if len(pkt) != N4Packet.SIZE:
            return None
        cmd, _, _ = struct.unpack("!BB6s", pkt)
        if cmd != N4Packet.CMD_READY:
            return None
        return True

    @staticmethod
    def exchange(ident: bytes) -> bytes:
        """Create EXCHANGE packet for NAT mapping discovery"""
        pkt = struct.pack(
            "!BB6s", N4Packet.CMD_EXCHG, N4Packet.RESERVED, ident[:6].ljust(6, b'\x00')
        )
        return pkt

    @staticmethod
    def dec_exchange(pkt: bytes) -> Optional[bytes]:
        """Decode EXCHANGE packet, returns ident or None"""
        if len(pkt) != N4Packet.SIZE:
            return None
        cmd, _, ident = struct.unpack("!BB6s", pkt)
        if cmd != N4Packet.CMD_EXCHG:
            return None
        return ident

    @staticmethod
    def peerinfo(peeraddr: Tuple[str, int]) -> bytes:
        """Create PEERINFO packet with peer's public address"""
        ip, port = peeraddr
        ipb = socket.inet_aton(ip)
        pkt = struct.pack(
            "!BB4sH", N4Packet.CMD_PINFO, N4Packet.RESERVED, ipb, port
        )
        return pkt

    @staticmethod
    def dec_peerinfo(pkt: bytes) -> Optional[Tuple[str, int]]:
        """Decode PEERINFO packet, returns (ip, port) or None"""
        if len(pkt) != N4Packet.SIZE:
            return None
        cmd, _, ipb, port = struct.unpack("!BB4sH", pkt)
        if cmd != N4Packet.CMD_PINFO:
            return None
        ip = socket.inet_ntoa(ipb)
        return (ip, port)

    @staticmethod
    def punch(ident: bytes) -> bytes:
        """Create PUNCH packet for hole punching"""
        pkt = struct.pack(
            "!BB6s", N4Packet.CMD_PUNCH, N4Packet.RESERVED, ident[:6].ljust(6, b'\x00')
        )
        return pkt

    @staticmethod
    def dec_punch(pkt: bytes) -> Optional[bytes]:
        """Decode PUNCH packet, returns ident or None"""
        if len(pkt) != N4Packet.SIZE:
            return None
        cmd, _, ident = struct.unpack("!BB6s", pkt)
        if cmd != N4Packet.CMD_PUNCH:
            return None
        return ident

    @staticmethod
    def is_n4_packet(data: bytes) -> bool:
        """Check if data is an N4 protocol packet (8 bytes, cmd 0x01-0x05)"""
        return len(data) == N4Packet.SIZE and data[0] in (0x01, 0x02, 0x03, 0x04, 0x05)


class N4Error:
    """N4 Error types"""

    class InvalidPacket(Exception):
        """Invalid packet format"""
        pass

    class PunchFailure(Exception):
        """Hole punching failed"""
        pass

    class Timeout(Exception):
        """Operation timeout"""
        pass
