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
