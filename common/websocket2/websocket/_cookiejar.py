import http.cookies
from typing import Optional, Dict

"""
_cookiejar.py
websocket - WebSocket client library for Python

Copyright 2025 engn33r

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""


class SimpleCookieJar:
    def __init__(self) -> None:
        self.jar: Dict[str, http.cookies.SimpleCookie] = {}

    def add(self, set_cookie: Optional[str]) -> None:
        if set_cookie:
            simple_cookie = http.cookies.SimpleCookie(set_cookie)

            for v in simple_cookie.values():
                domain = v.get("domain")
                if domain:
                    if not domain.startswith("."):
                        domain = "." + domain
                    cookie = self.jar.get(domain)
                    if cookie is None:
                        cookie = http.cookies.SimpleCookie()
                    cookie.update(simple_cookie)
                    self.jar[domain.lower()] = cookie

    def set(self, set_cookie: str) -> None:
        if set_cookie:
            simple_cookie = http.cookies.SimpleCookie(set_cookie)

            for v in simple_cookie.values():
                domain = v.get("domain")
                if domain:
                    if not domain.startswith("."):
                        domain = "." + domain
                    self.jar[domain.lower()] = simple_cookie

    def get(self, host: str) -> str:
        if not host:
            return ""

        cookies = []
        host_lower = host.lower()
        for domain in self.jar.keys():
            if host_lower.endswith(domain) or host_lower == domain[1:]:
                cookies.append(self.jar.get(domain))

        # 构建 cookie 字符串
        cookie_parts = []
        for cookie in cookies:
            if cookie is not None:
                for k, v in cookie.items():
                    cookie_parts.append("{}={}".format(k, v.value))

        # 去重并排序
        cookie_parts = sorted(set(cookie_parts))

        return "; ".join(filter(None, cookie_parts))