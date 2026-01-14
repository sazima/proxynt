"""
Tunnel Data Protocol for P2P Communication

Defines the packet format for multiplexed data over P2P tunnels.
"""
import struct
from typing import Tuple, Optional


class TunnelPacket:
    """
    Tunnel Data Packet Format:
    +------+-------+----------+---------+---------+
    | TYPE | FLAGS |   UID    | LENGTH  | PAYLOAD |
    | 1B   | 1B    |   4B     |   2B    |  ...    |
    +------+-------+----------+---------+---------+

    Header size: 8 bytes
    Max payload: 1400 bytes (UDP-safe)
    """

    # Packet types
    TYPE_DATA = 0x10           # Application data
    TYPE_DATA_ACK = 0x11       # Optional ACK for reliability
    TYPE_CLOSE_UID = 0x20      # Close specific UID stream
    TYPE_KEEPALIVE = 0x30      # Tunnel keepalive
    TYPE_KEEPALIVE_ACK = 0x31  # Keepalive response
    TYPE_TUNNEL_CLOSE = 0x40   # Close entire tunnel

    # Flags
    FLAG_NONE = 0x00
    FLAG_FRAGMENT = 0x01       # More fragments follow
    FLAG_LAST_FRAGMENT = 0x02  # Last fragment

    # Constants
    HEADER_SIZE = 8
    MAX_PAYLOAD = 1400
    MAX_PACKET_SIZE = HEADER_SIZE + MAX_PAYLOAD

    # Empty UID for control packets
    EMPTY_UID = b'\x00\x00\x00\x00'

    @staticmethod
    def pack_data(uid: bytes, data: bytes, flags: int = 0) -> bytes:
        """
        Pack a DATA packet

        Args:
            uid: 4-byte unique identifier for the stream
            data: payload data
            flags: optional flags

        Returns:
            Packed packet bytes
        """
        uid_4 = uid[:4].ljust(4, b'\x00')
        header = struct.pack('!BB4sH',
                             TunnelPacket.TYPE_DATA,
                             flags,
                             uid_4,
                             len(data))
        return header + data

    @staticmethod
    def pack_close_uid(uid: bytes) -> bytes:
        """Pack a CLOSE_UID packet to close a specific stream"""
        uid_4 = uid[:4].ljust(4, b'\x00')
        return struct.pack('!BB4sH',
                           TunnelPacket.TYPE_CLOSE_UID,
                           TunnelPacket.FLAG_NONE,
                           uid_4,
                           0)

    @staticmethod
    def pack_keepalive() -> bytes:
        """Pack a KEEPALIVE packet"""
        return struct.pack('!BB4sH',
                           TunnelPacket.TYPE_KEEPALIVE,
                           TunnelPacket.FLAG_NONE,
                           TunnelPacket.EMPTY_UID,
                           0)

    @staticmethod
    def pack_keepalive_ack() -> bytes:
        """Pack a KEEPALIVE_ACK packet"""
        return struct.pack('!BB4sH',
                           TunnelPacket.TYPE_KEEPALIVE_ACK,
                           TunnelPacket.FLAG_NONE,
                           TunnelPacket.EMPTY_UID,
                           0)

    @staticmethod
    def pack_tunnel_close() -> bytes:
        """Pack a TUNNEL_CLOSE packet"""
        return struct.pack('!BB4sH',
                           TunnelPacket.TYPE_TUNNEL_CLOSE,
                           TunnelPacket.FLAG_NONE,
                           TunnelPacket.EMPTY_UID,
                           0)

    @staticmethod
    def unpack(packet: bytes) -> Optional[Tuple[int, int, bytes, bytes]]:
        """
        Unpack a tunnel packet

        Args:
            packet: raw packet bytes

        Returns:
            Tuple of (type, flags, uid, payload) or None if invalid
        """
        if len(packet) < TunnelPacket.HEADER_SIZE:
            return None

        try:
            pkt_type, flags, uid, length = struct.unpack(
                '!BB4sH', packet[:TunnelPacket.HEADER_SIZE]
            )

            payload = packet[TunnelPacket.HEADER_SIZE:TunnelPacket.HEADER_SIZE + length]

            # Validate length
            if len(payload) != length:
                return None

            return (pkt_type, flags, uid, payload)
        except struct.error:
            return None

    @staticmethod
    def is_data_packet(pkt_type: int) -> bool:
        """Check if packet type is data"""
        return pkt_type == TunnelPacket.TYPE_DATA

    @staticmethod
    def is_control_packet(pkt_type: int) -> bool:
        """Check if packet type is a control packet"""
        return pkt_type in (
            TunnelPacket.TYPE_CLOSE_UID,
            TunnelPacket.TYPE_KEEPALIVE,
            TunnelPacket.TYPE_KEEPALIVE_ACK,
            TunnelPacket.TYPE_TUNNEL_CLOSE
        )

    @staticmethod
    def fragment_data(uid: bytes, data: bytes) -> list:
        """
        Fragment large data into multiple packets

        Args:
            uid: 4-byte unique identifier
            data: data to fragment

        Returns:
            List of packed packets
        """
        if len(data) <= TunnelPacket.MAX_PAYLOAD:
            return [TunnelPacket.pack_data(uid, data)]

        packets = []
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + TunnelPacket.MAX_PAYLOAD]
            is_last = (offset + TunnelPacket.MAX_PAYLOAD >= len(data))

            if is_last:
                flags = TunnelPacket.FLAG_LAST_FRAGMENT
            else:
                flags = TunnelPacket.FLAG_FRAGMENT

            packets.append(TunnelPacket.pack_data(uid, chunk, flags))
            offset += TunnelPacket.MAX_PAYLOAD

        return packets
