# -*- coding: utf-8 -*-

"""
KCP - A Better ARQ Protocol Implementation
skywind3000 (at) gmail.com, 2010-2011

Features:
+ Average RTT reduce 30% - 40% vs traditional ARQ like tcp.
+ Maximum RTT reduce three times vs tcp.
+ Lightweight, distributed as a single source file.

"""

__name__ = 'pykcp'
__author__ = 'skywind3000'
__author_email__ = ''
__license__ = 'MIT License'

from ctypes import c_uint32, c_int32
from collections import deque
from typing import overload

IKCP_RTO_NDL = 30       # no delay min rto
IKCP_RTO_MIN = 100      # normal min rto
IKCP_RTO_DEF = 200
IKCP_RTO_MAX = 60000
IKCP_CMD_PUSH = 81      # cmd: push data
IKCP_CMD_ACK  = 82      # cmd: ack
IKCP_CMD_WASK = 83      # cmd: window probe (ask)
IKCP_CMD_WINS = 84      # cmd: window size (tell)
IKCP_ASK_SEND = 1       # need to send IKCP_CMD_WASK
IKCP_ASK_TELL = 2       # need to send IKCP_CMD_WINS
IKCP_WND_SND = 32
IKCP_WND_RCV = 128      # must >= max fragment size
IKCP_MTU_DEF = 1400
IKCP_ACK_FAST   = 3
IKCP_INTERVAL   = 100
IKCP_OVERHEAD = 24
IKCP_DEADLINK = 20
IKCP_THRESH_INIT = 2
IKCP_THRESH_MIN = 2
IKCP_PROBE_INIT = 7000      # 7 secs to probe window size
IKCP_PROBE_LIMIT = 120000   # up to 120 secs to probe window
IKCP_FASTACK_LIMIT = 5      # max times to trigger fastack
IKCP_FASTACK_CONSERVE = 0

class uint32(int):
    def __new__(cls, value, *args, **kwargs):
        return super(uint32, uint32).__new__(cls, c_uint32(value).value)
    def __add__(self, other):
        return self.__class__(c_uint32(super(uint32, self).__add__(other)).value)
    def __sub__(self, other):
        return self.__class__(c_uint32(super(uint32, self).__sub__(other)).value)
    def __mul__(self, other):
        return self.__class__(c_uint32(super(uint32, self).__mul__(other)).value)
    def __truediv__(self, other):
        return self.__class__(c_uint32(int(super(uint32, self).__truediv__(other))).value)
    def __floordiv__(self, other):
        return self.__class__(c_uint32(super(uint32, self).__floordiv__(other)).value)
    def __mod__(self, other):
        return self.__class__(c_uint32(super(uint32, self).__mod__(other)).value)
    def __and__(self, other):
        return self.__class__(c_uint32(super(uint32, self).__and__(other)).value)
    def __or__(self, other):
        return self.__class__(c_uint32(super(uint32, self).__or__(other)).value)
    def __xor__(self, other):
        return self.__class__(c_uint32(super(uint32, self).__xor__(other)).value)
    def __invert__(self):
        return self.__class__(c_uint32(super(uint32, self).__invert__()).value)
    def __lshift__(self, _n):
        return self.__class__(c_uint32(super(uint32, self).__lshift__(_n)).value)
    def __rshift__(self, _n):
        return self.__class__(c_uint32(super(uint32, self).__rshift__(_n)).value)
    def __neg__(self):
        return self.__class__(c_uint32(super(uint32, self).__neg__()).value)
    def __pos__(self):
        return self.__class__(c_uint32(super(uint32, self).__pos__()).value)
    def __str__(self):
        return "%d" % int(self)
    def __repr__(self):
        return "%d <class uint32>" % int(self)

class int32(int):
    def __new__(cls, value, *args, **kwargs):
        return super(int32, int32).__new__(cls, c_int32(value).value)
    def __add__(self, other):
        return self.__class__(c_int32(super(int32, self).__add__(other)).value)
    def __sub__(self, other):
        return self.__class__(c_int32(super(int32, self).__sub__(other)).value)
    def __mul__(self, other):
        return self.__class__(c_int32(super(int32, self).__mul__(other)).value)
    def __truediv__(self, other):
        return self.__class__(c_int32(int(super(int32, self).__truediv__(other))).value)
    def __floordiv__(self, other):
        return self.__class__(c_int32(super(int32, self).__floordiv__(other)).value)
    def __mod__(self, other):
        return self.__class__(c_int32(super(int32, self).__mod__(other)).value)
    def __and__(self, other):
        return self.__class__(c_int32(super(int32, self).__and__(other)).value)
    def __or__(self, other):
        return self.__class__(c_int32(super(int32, self).__or__(other)).value)
    def __xor__(self, other):
        return self.__class__(c_int32(super(int32, self).__xor__(other)).value)
    def __invert__(self):
        return self.__class__(c_int32(super(int32, self).__invert__()).value)
    def __lshift__(self, _n):
        return self.__class__(c_int32(super(int32, self).__lshift__(_n)).value)
    def __rshift__(self, _n):
        return self.__class__(c_int32(super(int32, self).__rshift__(_n)).value)
    def __neg__(self):
        return self.__class__(c_int32(super(int32, self).__neg__()).value)
    def __pos__(self):
        return self.__class__(c_int32(super(int32, self).__pos__()).value)
    def __str__(self):
        return "%d" % int(self)
    def __repr__(self):
        return "%d <class int32>" % int(self)

def _itimediff(later, earlier):
    return int32(uint32(later) - earlier)

class IKCPSEG:
    """
    KCP packet object, which describes the relevant information of the packet
    """
    conv = uint32(0)         # Connection number: conv Used to represent which client from
    cmd  = uint32(0)         # Command word:
    frg  = uint32(0)         # Sheet number: User data may be divided into multiple KCP packages and send it out
    wnd  = uint32(0)         # Window size: The sender of the sender cannot exceed the value given by the receiver
    ts   = uint32(0)         # Timestamp
    sn   = uint32(0)         # serial number
    una  = uint32(0)         # Confirmation number: The next receiving serial number
    resendts = uint32(0)     # Reconstruction time stamp
    rto = uint32(0)          # Timeout time
    fastack = uint32(0)      # Quickly re -transmitting the mechanism, record the number of times the number of skipping, and the number of more than the number of times the rapid re -transmission
    xmit = uint32(0)         # Number of overwhelming times
    data = bytes()           # data

    def __init__(self, conv = 0, cmd = 0, frg = 0, wnd = 0, ts = 0, sn = 0, una = 0, data = bytes()):
        self.conv = uint32(conv)
        self.cmd = uint32(cmd)
        self.frg = uint32(frg)
        self.wnd = uint32(wnd)
        self.ts = uint32(ts)
        self.sn = uint32(sn)
        self.una = uint32(una)
        self.data = bytes(data)

    def __len__(self):
        return len(self.data)

    def __str__(self):
        return "conv:%d cmd:%02x frg:%d wnd:%d ts:%d sn:%d una:%d resendts:%d rto:%d fastack:%d xmit:%d data:%d" % \
            (self.conv, self.cmd, self.frg, self.wnd, self.ts, self.sn, self.una, self.resendts, self.rto, self.fastack, self.xmit, len(self.data))

    def __encode_data(self, buf, pos):
        end = pos + len(self.data)
        buf[pos : end] = self.data
        return end

    def total_size(self):
        return IKCP_OVERHEAD + len(self.data)

    def __encode_head(self, buf, pos):
        # little-endian
        def _encode32u(ptr, pos, v):
            ptr[pos + 0] = (v >> 0) & 0xFF
            ptr[pos + 1] = (v >> 8) & 0xFF
            ptr[pos + 2] = (v >> 16) & 0xFF
            ptr[pos + 3] = (v >> 24) & 0xFF
            return 4
        def _encode16u(ptr, pos, v):
            ptr[pos + 0] = (v >> 0) & 0xFF
            ptr[pos + 1] = (v >> 8) & 0xFF
            return 2
        def _encode8u(ptr, pos, v):
            ptr[pos + 0] = (v >> 0) & 0xFF
            return 1
        pos += _encode32u(buf, pos, self.conv)
        pos += _encode8u(buf, pos, self.cmd)
        pos += _encode8u(buf, pos, self.frg)
        pos += _encode16u(buf, pos, self.wnd)
        pos += _encode32u(buf, pos, self.ts)
        pos += _encode32u(buf, pos, self.sn)
        pos += _encode32u(buf, pos, self.una)
        pos += _encode32u(buf, pos, len(self.data))
        return pos

    @overload
    def encode_head(self, buf: bytearray, pos: int) -> int:
        ...

    @overload
    def encode_head(self) -> bytes:
        ...

    def encode_head(self, *args):
        if len(args) == 0:
            buffer = bytearray(IKCP_OVERHEAD)
            self.__encode_head(buffer, 0)
            return bytes(buffer)
        elif len(args) == 2:
            return self.__encode_head(args[0], args[1])
        else:
            raise TypeError('takes 2 positional arguments but %d were given' % len(args))

    @overload
    def encode(self, buf: bytearray, pos: int) -> int:
        ...

    @overload
    def encode(self) -> bytes:
        ...

    def encode(self, *args):
        if len(args) == 0:
            buffer = bytearray(IKCP_OVERHEAD + len(self.data))
            pos = self.__encode_head(buffer, 0)
            self.__encode_data(buffer, pos)
            return bytes(buffer)
        elif len(args) == 2:
            pos = self.__encode_head(args[0], args[1])
            return self.__encode_data(args[0], pos)
        else:
            raise TypeError('takes 2 positional arguments but %d were given' % len(args))

    @staticmethod
    def decode(buf, pos = 0):
        def _decode32u(ptr, _pos):
            l = (ptr[_pos + 3] & 0xFF)
            l = (ptr[_pos + 2] & 0xFF) + (l << 8)
            l = (ptr[_pos + 1] & 0xFF) + (l << 8)
            l = (ptr[_pos + 0] & 0xFF) + (l << 8)
            return (l, 4)
        def _decode16u(ptr, _pos):
            l = (ptr[_pos + 1] & 0xFF)
            l = (ptr[_pos + 0] & 0xFF) + (l << 8)
            return (l, 2)
        def _decode8u(ptr, _pos):
            l = (ptr[_pos + 0]) & 0xFF
            return (l, 1)
        # little-endian
        r = _decode32u(buf, pos); conv = r[0]; pos += r[1]
        r = _decode8u(buf, pos);  cmd = r[0]; pos += r[1]
        r = _decode8u(buf, pos);  frg = r[0]; pos += r[1]
        r = _decode16u(buf, pos); wnd = r[0]; pos += r[1]
        r = _decode32u(buf, pos); ts = r[0]; pos += r[1]
        r = _decode32u(buf, pos); sn = r[0]; pos += r[1]
        r = _decode32u(buf, pos); una = r[0]; pos += r[1]
        r = _decode32u(buf, pos); data_len = r[0]; pos += r[1]
        # type check
        if cmd != IKCP_CMD_PUSH and cmd != IKCP_CMD_ACK and \
                cmd != IKCP_CMD_WASK and cmd != IKCP_CMD_WINS:
            raise TypeError('Decoding unknown package type')
        # length check
        if (len(buf) < (pos + data_len)):
            raise IndexError('index out of range')
        seg = IKCPSEG(conv=conv, cmd=cmd, frg=frg, wnd=wnd, ts=ts, sn=sn, una=una, data=buf[pos : pos + data_len])
        return seg

class Kcp:
    """
    KCP packet object
    """
    def __init__(self, conv):
        """
        // create a new kcp control object, 'conv' must equal in two endpoint
        """
        self.conv = uint32(conv)                    # Connection number
        self.snd_una = uint32(0)                    # All the data before this number have been received
        self.snd_nxt = uint32(0)                    # The next bag to be sent
        self.rcv_nxt = uint32(0)                    # The next package sequence number to be received
        self.ts_recent = uint32(0)                  # Unused
        self.ts_lastack = uint32(0)                 # Unused
        self.ts_probe = uint32(0)                   # Special control frame sending timestamp
        self.probe_wait = uint32(0)                 # Special control frame sending waiting time
        self.snd_wnd = uint32(IKCP_WND_SND)         # Send a window
        self.rcv_wnd = uint32(IKCP_WND_RCV)         # Receiving window
        self.rmt_wnd = uint32(IKCP_WND_RCV)         # Remote receiving window size
        self.cwnd = uint32(0)                       # Actual sending window
        self.incr = uint32(0)                       # Current largest data traffic
        self.probe = uint32(0)                      # Special control frame to be sent for sending
        self.mtu = uint32(IKCP_MTU_DEF)             # Maximum length
        self.mss = uint32(self.mtu - IKCP_OVERHEAD) # Maximum data length of single -package data
        self.stream = False                         # Data stream mode, default is the data block
        # new kcp data buffer
        self.buffer = bytearray((self.mtu + IKCP_OVERHEAD) * 3)
        self.buflen = 0
        # init queue
        self.snd_queue = deque()                    # To send data queue
        self.rcv_queue = deque()                    # Receive data queue
        self.snd_buf = deque()
        self.rcv_buf = deque()                      # Receive data cache queue

        self.acklist = deque()                      # ACK queue
        self.state = uint32(0)                      # Whether the mark is a bad connection
        self.rx_srtt = int32(0)                     # When receiving ACK, calculate the current transmission delay
        self.rx_rttval = int32(0)                   # When receiving ACK, the average transmission delay is calculated
        self.rx_rto = int32(IKCP_RTO_DEF)           # Receive timeout time
        self.rx_minrto = int32(IKCP_RTO_MIN)        # Minimum receiving timeout
        self.current = uint32(0)                    # Current time stamp (millisecond)
        self.interval = uint32(IKCP_INTERVAL)       # Internal update timer interval (millisecond)
        self.ts_flush = uint32(IKCP_INTERVAL)       # The time stamp of the data next time
        self.nodly = 0
        self.updated = False                        # Whether the label has executed the Update function
        self.ssthresh = uint32(IKCP_THRESH_INIT)    # Slow start threshold
        self.fastresend = 0                         # Trigger the number of fast re -transmission times
        self.fastlimit = IKCP_FASTACK_LIMIT         # Trigger the limit of rapid re -transmission
        self.nocwnd = 0                             # Celection control mark
        self.xmit = uint32(0)                       # The number of re -transmission of this connection
        self.dead_link = uint32(IKCP_DEADLINK)      # The maximum number of retries, exceeding the maximum number of retries, marked as a bad connection

    def __str__(self):
        return "conv:%d"

    def output(self, buf):
        pass

    def recv(self, ispeek = False):
        peeksize = 0
        recover = False
        buf = bytes()
        # Check whether the queue is empty
        count = len(self.rcv_queue)
        if count <= 0:
            return buf
        # Get a pack of a pack of complete data
        peeksize = self.peeksize()
        if peeksize < 0:
            return buf
        # Check whether the length of the receiving queue is greater than the receiving window
        if len(self.rcv_queue) >= self.rcv_wnd:
            recover = True
        # merge fragment
        if ispeek:
            for item in self.rcv_queue:
                buf += item.data
                if item.frg == 0:
                    break
        else:
            while count > 0:
                item = self.rcv_queue.popleft()
                buf += item.data
                count -= 1
                if item.frg == 0:
                    break
        # move available data from rcv_buf -> rcv_queue
        while len(self.rcv_buf) > 0:
            seg = self.rcv_buf[0]
            if seg.sn == self.rcv_nxt and len(self.rcv_queue) < self.rcv_wnd:
                self.rcv_buf.popleft()
                self.rcv_queue.append(seg)
                self.rcv_nxt += 1
            else:
                break

        # fast recover
        if len(self.rcv_queue) < self.rcv_wnd and recover:
            # ready to send back IKCP_CMD_WINS in ikcp_flush
            # tell remote my window size
            self.probe |= uint32(IKCP_ASK_TELL)

        return buf

    def send(self, buf):
        buf = bytes(buf)
        if isinstance(buf, bytes) == False:
            raise TypeError("argument must be bytes-like object, not " + type(buf).__name__)
        buf_len = len(buf)
        # append to previous segment in streaming mode (if possible)
        if self.stream:
            if len(self.snd_queue) > 0:
                last_seg = self.snd_queue[-1]
                # Determine whether there is still room for the previous package of data
                if len(last_seg) < self.mss:
                    capacity = self.mss - len(last_seg)
                    extend = buf_len if buf_len < capacity else capacity
                    # Spin the data into the last pack of data and send it as a pack of data.
                    last_seg.data += buf[:extend]
                    buf = buf[extend:]
                    buf_len -= extend
            # Check whether there are remaining data
            if buf_len <= 0:
                return
        # Calculate the number of data packets
        if buf_len <= self.mss:
            count = 1
        else:
            count = int((buf_len + self.mss - 1) / self.mss)
        # Check whether the data package is too long, exceeding the maximum shard
        if count >= IKCP_WND_RCV:
            max_len = str(IKCP_WND_RCV * self.mss)
            raise BufferError("The length of buffer exceeds the maximum value. " + max_len)
        if count == 0:
            count = 1
        # fragment
        while count > 0:
            count -= 1
            size = self.mss if buf_len > self.mss else buf_len
            frg = count if not self.stream else 0
            seg = IKCPSEG(conv=self.conv, cmd=IKCP_CMD_PUSH, frg=frg, data=buf[:size])
            # Add to the end of the sending cache queue
            self.snd_queue.append(seg)
            buf = buf[size:]
            buf_len -= size

    def peeksize(self):
        # check the size of next message in the recv queue
        if len(self.rcv_queue) <= 0:
            return -1
        # Take out a bag from the queue head to check whether it is a slice package
        seg = self.rcv_queue[0]
        if seg.frg == 0:
            return len(seg)
        # Check whether the slide package is received
        if len(self.rcv_queue) < seg.frg + 1:
            return -1
        # Slawing, traversing the entire queue, obtaining the total size
        length = 0
        for piece in self.rcv_queue:
            length += len(piece)
            if piece.frg == 0:
                break
        return length

    def update(self, current):
        # Record the current millisecond time stamp
        self.current = uint32(current)
        if self.updated == False:
            self.updated = True
            self.ts_flush = self.current
        # Whether the interval between the acquisition time is 10 seconds
        slap = _itimediff(self.current, self.ts_flush)
        if slap >= 10000 or slap < -10000:
            self.ts_flush = self.current
            slap = 0
        # Reach the inspection interval, brush the data
        if slap >= 0:
            self.ts_flush += self.interval # Calculate the time for the next refresh data
            if _itimediff(self.current, self.ts_flush) >= 0:
                self.ts_flush = self.current + self.interval
            self.flush()

    def flush(self):
        # 'update' haven't been called.
        if self.updated == False:
            return
        def wnd_unused():
            r_len = len(self.rcv_queue)
            r_win = self.rcv_wnd
            if r_len < r_win:
                return r_win - r_len
            else:
                return 0
        current = self.current
        # Calculate the current window size
        curr_wnd = wnd_unused()
        # init buffer
        self.buflen = 0
        # flush acknowledges
        while len(self.acklist) > 0:
            ack = self.acklist.popleft()
            if self.buflen + IKCP_OVERHEAD > self.mtu:
                self.output(bytes(self.buffer[:self.buflen]))
                self.buflen = 0
            ack.wnd = curr_wnd
            ack.una = self.rcv_nxt
            self.buflen = ack.encode(self.buffer, self.buflen)
        # probe window size (if remote window size equals zero)
        if self.rmt_wnd == 0:
            if self.probe_wait == 0:
                self.probe_wait = uint32(IKCP_PROBE_INIT)   # Reset and detection time
                self.ts_probe = self.current + self.probe_wait  # The time stamp of the next detection
            else:
                # Check whether the current timestamp is greater than the detection timestamp
                if _itimediff(self.current, self.ts_probe) >= 0:
                    if self.probe_wait < IKCP_PROBE_INIT:
                        self.probe_wait = uint32(IKCP_PROBE_INIT)
                    self.probe_wait += self.probe_wait / 2
                    if self.probe_wait > IKCP_PROBE_LIMIT:
                        self.probe_wait = uint32(IKCP_PROBE_LIMIT)
                    self.ts_probe = self.current + self.probe_wait
                    self.probe |= uint32(IKCP_ASK_SEND)
        else:
            self.ts_probe = uint32(0)
            self.probe_wait = uint32(0)
        # flush window probing commands
        if self.probe & IKCP_ASK_SEND:
            # init wask seg
            wask = IKCPSEG(conv=self.conv, cmd=IKCP_CMD_WASK, wnd=curr_wnd, una=self.rcv_nxt)
            if self.buflen + IKCP_OVERHEAD > self.mtu:
                self.output(bytes(self.buffer[:self.buflen]))
                self.buflen = 0
            self.buflen = wask.encode(self.buffer, self.buflen)
        # flush window probing commands
        if self.probe & IKCP_ASK_TELL:
            # init tell seg
            tell = IKCPSEG(conv=self.conv, cmd=IKCP_CMD_WINS, wnd=curr_wnd, una=self.rcv_nxt)
            if self.buflen + IKCP_OVERHEAD > self.mtu:
                self.output(bytes(self.buffer[:self.buflen]))
                self.buflen = 0
            self.buflen = tell.encode(self.buffer, self.buflen)
        # probing completed, reset
        self.probe = uint32(0)
        # calculate window size
        cwnd = min(self.snd_wnd, self.rmt_wnd)
        if not self.nocwnd:
            cwnd = min(self.cwnd, cwnd)
        # move data from snd_queue to snd_buf
        while _itimediff(self.snd_nxt, self.snd_una + cwnd) < 0:
            if len(self.snd_queue) <= 0:
                break
            seg = self.snd_queue.popleft()
            # set seg
            seg.wnd = curr_wnd
            seg.ts = current
            seg.sn = self.snd_nxt
            seg.una = self.rcv_nxt
            seg.resendts = current
            seg.rto = self.rx_rto
            # append queue
            self.snd_buf.append(seg)
            self.snd_nxt += 1
        # calculate resent
        if self.fastresend > 0:
            resent = uint32(self.fastresend)
        else:
            resent = uint32(0xffffffff)
        if self.nodly == 0:
            rtomin = uint32(self.rx_rto >> 3)
        else:
            rtomin = uint32(0)
        # flush data segments
        lost = False
        change = 0
        for segment in self.snd_buf:
            needsend = False
            if segment.xmit == 0:
                needsend = True
                segment.xmit += 1
                segment.rto = self.rx_rto       # Receive timeout time
                segment.resendts = current + segment.rto + rtomin # Resend time
            elif _itimediff(current, segment.resendts) >= 0:
                needsend = True
                segment.xmit += 1
                self.xmit += 1
                if self.nodly == 0:
                    segment.rto += max(segment.rto, self.rx_rto)
                else:
                    step = segment.rto if self.nodly < 2 else self.rx_rto
                    segment.rto += step / 2
                segment.resendts = current + segment.rto
                lost = True
            elif segment.fastack >= resent:
                if segment.xmit <= self.fastlimit or self.fastlimit <= 0:
                    needsend = True
                    segment.xmit += 1
                    segment.fastack = uint32(0)
                    segment.resendts = current + segment.rto
                    change += 1

            if needsend:
                segment.ts = current
                segment.wnd = curr_wnd
                segment.una = self.rcv_nxt
                need = segment.total_size()

                if self.buflen + need > self.mtu:
                    self.output(bytes(self.buffer[:self.buflen]))
                    self.buflen = 0

                self.buflen = segment.encode(self.buffer, self.buflen)

                if segment.xmit >= self.dead_link:
                    self.state = -1
        # flash remain segments
        if self.buflen > 0:
            self.output(bytes(self.buffer[:self.buflen]))
            self.buflen = 0
        # update ssthresh
        if change > 0:
            inflight = self.snd_nxt - self.snd_una
            self.ssthresh = inflight / 2
            if self.ssthresh < IKCP_THRESH_MIN:
                self.ssthresh = uint32(IKCP_THRESH_MIN)
            self.cwnd = self.ssthresh + resent
            self.incr = self.cwnd * self.mss
        # handle lost
        if lost:
            self.ssthresh = uint32(cwnd / 2)
            if self.ssthresh < IKCP_THRESH_MIN:
                self.ssthresh = IKCP_THRESH_MIN
            self.cwnd = uint32(1)
            self.incr = self.mss
        # handle windows
        if self.cwnd < 1:
            self.cwnd = uint32(1)
            self.incr = self.mss

    def parse_data(self, newseg):
        sn = newseg.sn
        repeat = 0
        index = 0

        if _itimediff(sn, self.rcv_nxt + self.rcv_wnd) >= 0 or _itimediff(sn, self.rcv_nxt) < 0:
            return
        # Check whether it is a duplicate package
        for item in self.rcv_buf:
            if item.sn == sn:
                repeat = 1
                break
            if item.sn > sn:
                break
            index += 1
        # Insert the unreproducible package to the behind the queue
        if repeat == 0:
            # Insert
            self.rcv_buf.insert(index, newseg)
        # move available data from rcv_buf -> rcv_queue
        while len(self.rcv_buf) > 0:
            seg = self.rcv_buf[0]
            if seg.sn == self.rcv_nxt and len(self.rcv_queue) < self.rcv_wnd:
                self.rcv_buf.popleft()
                self.rcv_queue.append(seg)
                self.rcv_nxt += 1
            else:
                break

    def input(self, data):
        # input data
        if isinstance(data, bytes) == False:
            raise TypeError("argument must be bytes-like object, not " + type(data).__name__)
        # sn analytic function
        def parse_una(_una):
            while len(self.snd_buf) > 0:
                if _itimediff(_una, self.snd_buf[0].sn) > 0:
                    self.snd_buf.popleft()
                else:
                    break
        # shrink function
        def shrink_buf():
            if len(self.snd_buf) > 0:
                self.snd_una = self.snd_buf[0].sn
            else:
                self.snd_una = self.snd_nxt
        # update ack function
        def update_ack(rtt):
            if self.rx_srtt == 0:
                self.rx_srtt = rtt
                self.rx_rttval = rtt / 2
            else:
                delta = rtt - self.rx_srtt
                if delta < 0:
                    delta = -delta
                self.rx_rttval = (int32(3 * self.rx_rttval) + delta) / 4
                self.rx_srtt = (int32(7 * self.rx_srtt) + rtt) / 8
                if self.rx_srtt < 1:
                    self.rx_srtt = 1
            rto = self.rx_srtt + max(self.interval, int32(4 * self.rx_rttval))
            self.rx_rto = int32(min(max(self.rx_minrto, rto), int32(IKCP_RTO_MAX)))
        # parse ack
        def parse_ack(sn):
            if _itimediff(sn, self.snd_una) < 0 or _itimediff(sn, self.snd_nxt) >= 0:
                return
            for item in self.snd_buf:
                if sn == item.sn:
                    self.snd_buf.remove(item)
                    break
                if _itimediff(sn, item.sn) < 0:
                    break
        # parse fast ack
        def parse_fastack(sn, ts):
            if _itimediff(sn, self.snd_una) < 0 or _itimediff(sn, self.snd_nxt) >= 0:
                return
            for s in self.snd_buf:
                if _itimediff(sn, s.sn) < 0:
                    break
                elif sn != s.sn:
                    if IKCP_FASTACK_CONSERVE:
                        s.fastack += 1
                    else:
                        if _itimediff(ts, s.ts) >= 0:
                            s.fastack += 1

        prev_una = self.snd_una
        # Parse the received data
        size = len(data)
        buf = data
        pos = 0
        flag = False
        maxack = 0
        latest_ts = 0
        while size - pos >= IKCP_OVERHEAD:
            try:
                # new segment package
                seg = IKCPSEG.decode(buf, pos)
            except:
                return
            # Update data buffer reading offset
            pos += seg.total_size()
            if seg.conv != self.conv:
                return
            # update remote window size
            self.rmt_wnd = seg.wnd
            # Check the remote UNA and the local to send queue,
            # and discard the data serial number in the locally waiting queue is smaller than the receiving serial number at the remote end
            parse_una(seg.una)
            # renew
            shrink_buf()
            # processing command
            if seg.cmd == IKCP_CMD_ACK:
                t = _itimediff(self.current, seg.ts)
                if t >= 0:
                    update_ack(int32(t))
                parse_ack(seg.sn)
                shrink_buf()
                if flag == False:
                    flag = True
                    maxack = seg.sn
                    latest_ts = seg.ts
                else:
                    if _itimediff(seg.sn, maxack) > 0:
                        if IKCP_FASTACK_CONSERVE:
                            maxack = seg.sn
                            latest_ts = seg.ts
                        else:
                            if _itimediff(seg.ts, latest_ts) > 0:
                                maxack = seg.sn
                                latest_ts = seg.ts
            elif seg.cmd == IKCP_CMD_PUSH:
                if _itimediff(seg.sn, self.rcv_nxt + self.rcv_wnd) < 0:
                    # Receive data packets falling in the window
                    ack_seg = IKCPSEG(conv=seg.conv, cmd=IKCP_CMD_ACK, frg=seg.frg, ts=seg.ts, sn=seg.sn)
                    self.acklist.append(ack_seg)
                if _itimediff(seg.sn, self.rcv_nxt) >= 0:
                    self.parse_data(seg)
            elif seg.cmd == IKCP_CMD_WASK:
                # ready to send back IKCP_CMD_WINS in ikcp_flush
                # tell remote my window size
                self.probe |= uint32(IKCP_ASK_TELL)
            elif seg.cmd == IKCP_CMD_WINS:
                # do nothing
                pass
            else:
                return

        if flag:
            parse_fastack(maxack, latest_ts)

        if _itimediff(self.snd_una, prev_una) > 0:
            if self.cwnd < self.rmt_wnd:
                mss = self.mss
                if self.cwnd < self.ssthresh:
                    self.cwnd += 1
                    self.incr += mss
                else:
                    if self.incr < mss:
                        self.incr = mss
                    self.incr += (mss * mss) / self.incr + (mss / 16)
                    if (self.cwnd + 1) * mss <= self.incr:
                        if True:
                            self.cwnd = (self.incr + mss - 1) / (mss if (mss > 0) else uint32(1))
                        else:
                            self.cwnd += 1

                if self.cwnd > self.rmt_wnd:
                    self.cwnd = self.rmt_wnd
                    self.incr = self.rmt_wnd * mss

    def check(self, current):
        """
        Determine when should you invoke update:
        returns when you should invoke update in millisec, if there
        is no ikcp input/send calling. you can call update in that
        time, instead of call update repeatly.
        Important to reduce unnacessary update invoking. use it to
        schedule update (eg. implementing an epoll-like mechanism,
        or optimize update when handling massive kcp connections)
        """
        if self.updated == False:
            return current
        ts_flush = self.ts_flush
        # Check immediately after 10 seconds
        if _itimediff(current, self.ts_flush) >= 10000 or \
                _itimediff(current, self.ts_flush) < -1000:
            ts_flush = current
        if _itimediff(current, ts_flush) >= 0:
            return current
        # Find the nearest node time
        tm_packet = 0x7fffffff
        tm_flush = _itimediff(ts_flush, current)
        for seg in self.snd_buf:
            diff = _itimediff(seg.resendts, current)
            if diff <= 0:
                return current
            if diff < tm_packet:
                tm_packet = diff
        minimal = min(tm_packet, tm_flush)
        if minimal >= self.interval:
            minimal = self.interval
        return current + minimal

    def setmtu(self, mtu):
        # change MTU size, default is 1400
        if mtu < 50 or mtu < IKCP_OVERHEAD:
            min_str = str(max(50, IKCP_OVERHEAD))
            raise BufferError("MtU must be greater than" + min_str)
        self.mtu = uint32(mtu)
        self.mss = self.mtu - uint32(IKCP_OVERHEAD)
        self.buffer = bytearray((self.mtu + IKCP_OVERHEAD) * 3)
        self.buflen = 0

    def wndsize(self, sndwnd, rcvwnd):
        # set maximum window size: sndwnd=32, rcvwnd=32 by default
        if sndwnd > 0:
            self.snd_wnd = uint32(sndwnd)
        if rcvwnd > 0: # must >= max fragment size
            self.rcv_wnd = uint32(max(rcvwnd, IKCP_WND_RCV))

    def waitsnd(self):
        # get how many packet is waiting to be sent
        return len(self.snd_buf) + len(self.snd_queue)

    def nodelay(self, nodly, interval, resend, nc):
        """
        fastest: nodelay(1, 20, 2, 1)
        nodly: 0:disable(default), 1:enable
        interval: internal update timer interval in millisec, default is 100ms
        resend: 0:disable fast resend(default), 1:enable fast resend
        nc: 0:normal congestion control(default), 1:disable congestion control
        """
        if nodly >= 0:
            self.nodly = nodly
            if nodly != 0:
                self.rx_minrto = int32(IKCP_RTO_NDL)
            else:
                self.rx_minrto = int32(IKCP_RTO_MIN)
        if interval >= 0:
            if interval > 5000:
                interval = 5000
            elif interval < 10:
                interval = 10
            self.interval = uint32(interval)
        if resend >= 0:
            self.fastresend = resend
        if nc >= 0:
            self.nocwnd = nc

    @staticmethod
    def getconv(data):
        # read conv
        return (data[3] << 24 | data[2] << 16  | data[1] << 8 | data[0] << 0) & 0xFFFFFFFF