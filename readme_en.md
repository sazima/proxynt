# 

ProxyNT is a reverse proxy server that can expose a local server to the internet through NATs and firewalls
## Principle

![原理](https://i.imgtg.com/2023/02/08/cqhoI.png)


## Features
- Easily manage port forwarding from anywhere via a browser
- Uses WebSocket encryption for secure communication between the public server and local client
- Easy to install with pip.
- Stable, automatically reconnects, and has been used in production environments

## Common Use Cases

1. Host web servers from home
2. Manage IoT devices

## Installation

```bash
pip install proxynt
```


## Usage
Client
```bash
nt_client --help
# start client
nt_client -c config_c.json
```
Server

```bash
# view help
nt_server --help
# start server
nt_server -c config_s.json
```
After starting the server, open the management page in a browser:
```bash
Copy code
The management page URL is the WebSocket path plus /admin,
for example:
http://192.168.9.224:18888/websocket_path/admin
```
![ui](https://i.imgtg.com/2023/02/08/cqirD.png)

## Example: Accessing an Internal Network Machine via SSH

Suppose the public server's IP is 192.168.9.224.

#### 1. Configure config_s.json on the public server


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
Explanation:

- port: listening port
- password: connection password
- path: WebSocket path
- admin: management page configuration (not required)
- admin.enable: whether to enable the management page
- admin.admin_password: management password

Then start the server with:
```bash
nt_server -c config_s.json
```

#### 2. Configure config_c.json on the machine to be accessed

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

Explanation:

- `server`: configuration for the server to connect to, including port, IP address, whether to use HTTPS, password, and WebSocket path.
- `client_name`: name of the client, which must be unique.
- `client`: list of port forwarding configurations, mapping port 22 on the local machine to port 12222 on the server in this example.

Then start the client with:
```bash
nt_client -c config_c.json
```

#### 3. SSH connection

```
ssh -oPort=12222 test@192.168.9.224
```

#### Open the management page

```
http://192.168.9.224:18888/websocketpath/admin
```

## Full Configuration Description (please delete the comments when using)


- Client config_c.json

```json

{
  "server": {  // Server configuration to connect to
    "port": 18888,  // Port
    "host": "192.168.9.224",  // IP address
    "https": false,  // Whether the server is using HTTPS
    "password": "helloworld",  // Password
    "path": "/websocket_path"  // WebSocket path
  },
  "client": [  // List of forwarding configurations
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
  "client_name": "ubuntu1",  // Client name, must be unique
  "log_file": "/var/log/nt/nt.log"  // Path to log file
}

```



- Server config_c.json

```json

{
    "port": 18888,  // Listening port
    "password": "helloworld",  // Password
    "path": "/websocket_path",  // WebSocket path
    "log_file": "/var/log/nt/nt.log",  // Path to log file
    "admin": {  
        "enable": true,  // Whether to enable admin page
        "admin_password": "new_password"  // Password for admin page
    }
}

```
## Update history

- 1.1.6: Fixed client WebSocketException: socket is already opened.







