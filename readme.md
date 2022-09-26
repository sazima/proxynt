# 内网穿透工具

客户端
```
python run_client.py -c config_c.json
```

服务端
```
python run_server.py -c config_s.json
```


## 示例, 通过 SSH 访问内网机器

假设公网机器的ip是 `192.168.9.224`

#### 1. 在公网机器上配置`config_s.json`, 设置连接密码和接受客户端配置的端口
```json
{
  "port": 18888,
  "password": "helloworld"
}
```
然后启动: 
`python run_server.py -c config_s.json `

#### 2. 在需要被访问的电脑上配置`config_c.json`
 配置config_c.json
```json
{
  "server": {
    "port": 18888,
    "host": "192.168.9.224",
    "https": false,
    "password": "helloworld"
  },
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

然后启动: 
`python run_client.py -c config_c.json`

#### 3. ssh 连接: 
```
ssh -oPort=12222 test@192.168.9.224
```
