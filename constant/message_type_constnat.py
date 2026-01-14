class MessageTypeConstant:
    PUSH_CONFIG = '1'

    ERROR = '3'

    WEBSOCKET_OVER_TCP = '2'
    REQUEST_TO_CONNECT = '5'

    PING = '4'

    WEBSOCKET_OVER_UDP = '6'
    REQUEST_TO_CONNECT_UDP = '7'

    # 乐观发送模式：连接状态消息
    CONNECT_CONFIRMED = '8'  # 连接确认（客户端成功连接本地服务）
    CONNECT_FAILED = '9'     # 连接失败（客户端连接本地服务失败）

    # 客户端到客户端转发
    CLIENT_TO_CLIENT_FORWARD = 'a'  # 客户端请求转发到另一个客户端

    # P2P hole punching
    P2P_OFFER = 'b'           # Client A requests P2P connection with client B
    P2P_ANSWER = 'c'          # Client B responds to P2P request
    P2P_CANDIDATE = 'd'       # Exchange NAT traversal information
    P2P_SUCCESS = 'e'         # P2P connection established successfully
    P2P_FAILED = 'f'          # P2P connection failed, fallback to relay

    P2P_PRE_CONNECT = 'g'
    P2P_EXCHANGE = 'h'        # UDP exchange to establish NAT mapping
    P2P_PEER_INFO = 'i'       # Server sends peer's actual UDP port
    P2P_PUNCH_REQUEST = 'j'   # Request to initiate P2P hole punching