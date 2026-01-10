ProxyNT 是一个反向代理服务器，可以透过NAT和防火墙将本地服务器暴露到公网上

## [English Readme](./readme_en.md)
# 内网穿透工具
- github: https://github.com/sazima/proxynt
- gitee: https://gitee.com/sazima1/proxynt

## 原理

![原理](./preview.png)

## 特性

1. 随时随地打开浏览器管理端口映射
2. 公网服务器和内网客户端之间使用 WebSocket 加密传输
3. 依赖少, 使用 pip一键安装
4. 稳定, 自动重连, 已在生产环境中使用
5. 支持限速
6. 支持 TCP 和 `UDP 协议
7. 支持客户端到客户端（C2C）转发，无需在目标端暴露公网端口
8. 支持 P2P 打洞直连，`降低延迟和服务器带宽消耗
9. 智能回退机制，P2P 失败时自动使用服务器中转

## 常用场景
1. 在家托管网站服务器
2. 管理物联网设备
3. 远程访问家里/公司的其他内网机器（C2C 转发）
4. 多地内网设备互联互通

## 安装

```
pip install -U proxynt
```
如果需要安装安卓端 [点击](https://github.com/sazima/proxynt_android)

## 使用教程

假设公网服务器的 IP 是 `192.168.9.224`

### 准备工作

**1. 在公网服务器上启动服务端**

创建 `config_s.json`：
```json
{
  "port": 18888,
  "path": "/websocket_path",
  "password": "helloworld",
  "admin": {
    "enable": true,
    "admin_password": "admin123"
  }
}
```

启动服务端：
```bash
nt_server -c config_s.json
```

---

## 示例一、将端口暴露在公网

适用场景：让公网上的任何人都能访问你的内网服务（如个人网站、远程桌面等）

**步骤 1：在内网机器上启动客户端**

创建 `config_c.json`：
```json
{
  "server": {
    "url": "ws://192.168.9.224:18888/websocket_path",
    "password": "helloworld"
  },
  "client_name": "home_pc"
}
```

启动客户端：
```bash
nt_client -c config_c.json
```

**步骤 2：Web 界面配置端口映射**

1. 打开浏览器访问：`http://192.168.9.224:18888/websocket_path/admin`
2. 输入管理密码 `admin123` 登录
3. 找到客户端 `home_pc`，点击"转发配置"按钮
4. 填写配置：
   - 名称：ssh
   - 远程端口：12222
   - 本地 IP：127.0.0.1
   - 本地端口：22
   - 协议：TCP
5. 点击确定

**步骤 3：通过公网访问**

现在任何人都可以通过公网 IP 访问你的内网服务：
```bash
ssh -p 12222 user@192.168.9.224
```

---

## 示例一、安全地访问内网服务（无需暴露公网端口）

适用场景：在办公室访问家里的电脑，或让朋友访问你的内网服务，但不想暴露在公网上

**原理**：访问者和服务提供者都运行客户端，通过服务器建立安全连接，支持 P2P 打洞加速

**示例**：在办公室（机器 A）访问家里（机器 B）的 SSH

**步骤 1：两台机器都启动客户端**

机器 A（办公室）的 `config_c.json`：
```json
{
  "server": {
    "url": "ws://192.168.9.224:18888/websocket_path",
    "password": "helloworld"
  },
  "client_name": "office_pc"
}
```

机器 B（家里）的 `config_c.json`：
```json
{
  "server": {
    "url": "ws://192.168.9.224:18888/websocket_path",
    "password": "helloworld"
  },
  "client_name": "home_pc"
}
```

两台机器都启动客户端：
```bash
nt_client -c config_c.json
```

**步骤 2：Web 界面配置 C2C 规则**

1. 打开管理页面：`http://192.168.9.224:18888/websocket_path/admin`
2. 点击任一客户端右侧的"C2C 规则"按钮
3. 填写配置：
   - 规则名称: 随便， 比如: abc
   - 源客户端：office_pc（访问方）
   - 目标客户端：home_pc（被访问方）
   - 连接模式：直连模式
   - 目标 IP：127.0.0.1
   - 目标端口：22
   - 本地 IP：127.0.0.1
   - 本地端口：12222
   - 协议：TCP
   - 启用 P2P：是（可选，启用后自动尝试直连，失败会自动回退）
4. 点击确定

**步骤 3：在办公室访问家里的电脑**

在办公室（机器 A）上：
```bash
ssh -p 12222 user@127.0.0.1
```

就可以安全地访问家里电脑的 SSH 了！

**特点**：
- ✅ 家里电脑无需暴露任何公网端口
- ✅ 只有你能访问，其他人无法访问
- ✅ 启用 P2P 后延迟更低（自动打洞，失败自动回退）


## 高级配置参考

大部分功能都可以通过 Web 界面配置，以下是完整的配置文件参考（适用于需要通过配置文件管理的场景）。

<details>
<summary>点击展开查看完整配置示例</summary>

### 客户端配置 (config_c.json)
```json
{
  "server": {
    "url": "ws://192.168.9.224:18888/websocket_path",
    "password": "helloworld"
  },
  "client_name": "my_computer",
  "log_file": "/var/log/nt/nt.log"
}
```

**说明**：
- `server.url`: 服务器 WebSocket 地址（ws:// 或 wss://）
- `server.password`: 连接密码
- `client_name`: 客户端名称（必须唯一）
- `log_file`: 日志文件路径（可选）
- 端口映射建议在 Web 界面配置，无需写在配置文件中

### 服务端配置 (config_s.json)
```json
{
  "port": 18888,
  "password": "helloworld",
  "path": "/websocket_path",
  "log_file": "/var/log/nt/nt.log",
  "admin": {
    "enable": true,
    "admin_password": "admin123"
  }
}
```

**说明**：
- `port`: 监听端口
- `password`: 连接密码
- `path`: WebSocket 路径
- `admin.enable`: 是否启用 Web 管理界面
- `admin.admin_password`: 管理界面密码
- C2C 规则建议在 Web 界面配置，无需写在配置文件中

</details>

## 趋势

[![Stargazers over time](https://starchart.cc/sazima/proxynt.svg)](https://starchart.cc/sazima/proxynt)


## 更新记录
- 2.0.3: 新增 P2P 打洞功能，优化 C2C 直连延迟
- 2.0.2: 新增 C2C 直连模式，无需在目标端暴露端口
- 2.0.1: 支持客户端到客户端（C2C）转发
- 2.0.0: 支持 UDP 协议转发
- 1.1.9: 端口限速
- 1.1.8: 管理页显示客户端版本
- 1.1.7: 修复服务端处理重复client_name
- 1.1.6: 修复客户端 WebSocketException: socket is already opened