# 内网穿透工具
- github: https://github.com/sazima/proxynt
- gitee: https://gitee.com/sazima1/proxynt
## 原理

![原理](https://i.imgtg.com/2023/02/08/cqhoI.png)

## 特性

1. 随时随地打开浏览器管理端口映射
2. 公网服务器和内网客户端之间使用 WebSocket 加密传输
3. 依赖少, 使用 pip一键安装
4. 稳定, 自动重连, 已在生产环境中使用

## 常用场景
1. 在家托管网站服务器
2. 管理物联网设备

## 安装

```
pip install proxynt
```

## 使用

客户端
```
# 查看帮助
nt_client --help
# 启动客户端
nt_client -c config_c.json
```

服务端
```
# 查看帮助
nt_server --help
# 启动服务端
nt_server -c config_s.json
```

服务端ui
```
管理页面路径为websocket路径+/admin,
比如 
http://192.168.9.224:18888/websocketpath/admin
```


![ui](https://i.imgtg.com/2023/02/08/cqirD.png)

## 示例, 通过 SSH 访问内网机器

假设公网机器的ip是 `192.168.9.224`

#### 1. 在公网机器上配置`config_s.json`

```json
{
  "port": 18888,
  "password": "helloworld",
  "path": "/websocket_path",
  "admin": {
    "enable": true,  
    "admin_password": "new_password"  
  }
}
```

说明: 
- `port`: 监听端口
- `password`: 连接密码
- `path`: websocket路径
- `admin`: 管理页配置(非必须)
- `admin.enable`: 是否启用管理页
- `admin.admin_password`: 管理密码

然后启动:
`nt_server -c config_s.json `

#### 2. 在需要被访问的内网电脑上配置`config_c.json`

配置config_c.json
 
```json
{
  "server": {
    "port": 18888,
    "host": "192.168.9.224",
    "https": false,
    "password": "helloworld",
    "path": "/websocket_path"
  },
  "client_name": "我家里的小电脑",
  "client": [
    {
      "name": "ssh1",
      "remote_port": 12222,
      "local_port": 22,
      "local_ip": "127.0.0.1"
    }
  ]
}
```
说明:
- `server`: 要连接的服务器端口, ip, 是否是https, 密码, websocket路径
- `client_name`: 客户端名称
- `client`:  这里是将本机的22端口映射到服务器的12222端口上

然后启动: 
`nt_client -c config_c.json`

#### 3. ssh 连接: 
```
ssh -oPort=12222 test@192.168.9.224
```


#### 打开管理页面:

```
http://192.168.9.224:18888/websocketpath/admin
```

## 完整配置说明

- 客户端 config_c.json
```json5
{
  "server": {  // 要连接的服务端配置
    "port": 18888,  // 端口
    "host": "192.168.9.224",  // 端ip
    "https": false,  //服务端是否启动https
    "password": "helloworld",  // 密码
    "path": "/websocket_path"  // websocket 路径
  },
  "client": [  // 转发的配置列表
    {
      "name": "ssh",
      "remote_port": 1222,
      "local_port": 22,
      "local_ip": "127.0.0.1"
    },
    {
      "name": "mongo",
      "remote_port": 1223,
      "local_port": 27017,
      "local_ip": "127.0.0.1"
    }
  ],
  "client_name": "ubuntu1",  // 客户端名称, 要唯一
  "log_file": "/var/log/nt/nt.log"  // 日志路径
}
```


- 服务端 config_c.json
```json5
{
    "port": 18888,  // 监听端口
    "password": "helloworld",  // 密码
    "path": "/websocket_path",  // websocket路径
    "log_file": "/var/log/nt/nt.log",  // 日志路径
    "admin": {  
        "enable": true,  // 是否启用管理页
        "admin_password": "new_password"  // 管理页密码
    }
}
```

## 更新记录
- 1.1.5: 修复客户端 WebSocketException: socket is already opened