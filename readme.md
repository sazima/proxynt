
## [English Readme](./readme_en.md)

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
5. 支持限速

## 常用场景
1. 在家托管网站服务器
2. 管理物联网设备

## 安装

```
pip install -U proxynt
```

## 使用示例, 通过 SSH 访问内网机器

假设公网机器的ip是 `192.168.9.224`

#### 1. 在公网机器上新建`config_s.json`文件

`config_s.json`内容:
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
然后启动:
`nt_server -c config_s.json `

说明: 
- `port`: 监听端口
- `password`: 连接密码
- `path`: websocket路径
- `admin`: 管理页配置(非必须)
- `admin.enable`: 是否启用管理页
- `admin.admin_password`: 管理密码


#### 2. 在需要被访问的内网电脑上新建`config_c.json`文件

config_c.json内容:
```json
{
  "server": {
    "port": 18888,
    "host": "192.168.9.224",
    "https": false,
    "password": "helloworld",
    "path": "/websocket_path"
  },
  "client_name": "home_pc",
  "client": []
}
```

然后启动:
`nt_client -c config_c.json`

说明:
- `server`: 要连接的服务器端口, ip, 是否是https, 密码, websocket路径
- `client_name`: 客户端名称
- `client`:  空数组

#### 3. 打开服务端网页 `http://192.168.9.224:18888/websocket_path/admin` 添加端口:

![VWCvq.md.png](https://i.imgtg.com/2023/02/27/VWCvq.md.png)

说明: 管理页面路径为 **websocket路径 + /admin**

#### 4. 配置成功, 使用 ssh 连接:
```
ssh -oPort=12222 test@192.168.9.224
```


## 完整配置说明(使用请删除注释)

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
  "client": [  // 转发的配置列表(这里可以不填, 在服务端网页上配置)
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
## 趋势

[![Stargazers over time](https://starchart.cc/sazima/proxynt.svg)](https://starchart.cc/sazima/proxynt)


## 更新记录
- 1.1.9: 端口限速
- 1.1.8: 管理页显示客户端版本
- 1.1.7: 修复服务端处理重复client_name
- 1.1.6: 修复客户端 WebSocketException: socket is already opened

## TODO
- ui美化
- 视频教程
