import random
import secrets
import time
from typing import Dict, Tuple

"""
登录验证码 (纯标准库生成 SVG) 与按 IP 的登录失败锁定
只在 tornado 线程中使用, 不加锁
"""

CAPTCHA_CHARS = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # 去掉 0/O、1/I/L 等易混淆字符
CAPTCHA_LENGTH = 4
CAPTCHA_WIDTH = 120
CAPTCHA_HEIGHT = 44
CAPTCHA_EXPIRE_SECONDS = 120
CAPTCHA_MAX_STORE = 1000

LOGIN_MAX_FAILURES = 5  # 同一 IP 连续失败次数上限
LOGIN_LOCK_SECONDS = 300
LOGIN_MAX_TRACKED_IPS = 10000


def _rand_color(low: int, high: int) -> str:
    return '#%02x%02x%02x' % (random.randint(low, high), random.randint(low, high), random.randint(low, high))


def _render_svg(code: str) -> str:
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{CAPTCHA_WIDTH}" height="{CAPTCHA_HEIGHT}" '
             f'viewBox="0 0 {CAPTCHA_WIDTH} {CAPTCHA_HEIGHT}">',
             f'<rect width="100%" height="100%" fill="{_rand_color(225, 250)}"/>']
    # 噪点
    for _ in range(30):
        parts.append(f'<circle cx="{random.uniform(0, CAPTCHA_WIDTH):.1f}" cy="{random.uniform(0, CAPTCHA_HEIGHT):.1f}" '
                     f'r="{random.uniform(0.5, 1.5):.1f}" fill="{_rand_color(100, 200)}"/>')
    # 字符: 随机字号、旋转、偏移
    step = CAPTCHA_WIDTH / (CAPTCHA_LENGTH + 1)
    for i, ch in enumerate(code):
        x = step * (i + 0.6) + random.uniform(-4, 4)
        y = CAPTCHA_HEIGHT * 0.68 + random.uniform(-3, 3)
        parts.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{random.randint(22, 26)}" font-family="Arial,sans-serif" '
                     f'font-weight="bold" fill="{_rand_color(20, 120)}" '
                     f'transform="rotate({random.randint(-25, 25)} {x:.1f} {y:.1f})">{ch}</text>')
    # 干扰曲线
    for _ in range(3):
        parts.append(f'<path d="M{random.uniform(0, 20):.1f} {random.uniform(0, CAPTCHA_HEIGHT):.1f} '
                     f'Q{random.uniform(30, 90):.1f} {random.uniform(-10, CAPTCHA_HEIGHT + 10):.1f} '
                     f'{random.uniform(100, CAPTCHA_WIDTH):.1f} {random.uniform(0, CAPTCHA_HEIGHT):.1f}" '
                     f'stroke="{_rand_color(60, 160)}" stroke-width="{random.uniform(1, 2):.1f}" fill="none"/>')
    parts.append('</svg>')
    return ''.join(parts)


class CaptchaStore:
    """captcha_id -> (答案, 生成时间), 校验一次即作废"""

    def __init__(self):
        self._store: Dict[str, Tuple[str, float]] = dict()

    def create(self) -> Tuple[str, str]:
        """返回 (captcha_id, svg)"""
        if len(self._store) >= CAPTCHA_MAX_STORE:
            self._clean()
        if len(self._store) >= CAPTCHA_MAX_STORE:
            self._store.pop(next(iter(self._store)))  # 仍然满则淘汰最早的
        code = ''.join(secrets.choice(CAPTCHA_CHARS) for _ in range(CAPTCHA_LENGTH))
        captcha_id = secrets.token_urlsafe(16)
        self._store[captcha_id] = (code, time.time())
        return captcha_id, _render_svg(code)

    def verify(self, captcha_id: str, answer: str) -> bool:
        if not captcha_id or not answer:
            return False
        item = self._store.pop(captcha_id, None)
        if item is None:
            return False
        code, create_time = item
        if time.time() - create_time > CAPTCHA_EXPIRE_SECONDS:
            return False
        return secrets.compare_digest(code, answer.strip().upper())

    def _clean(self):
        now = time.time()
        for k in [k for k, (_, t) in self._store.items() if now - t > CAPTCHA_EXPIRE_SECONDS]:
            self._store.pop(k, None)


class LoginLimiter:
    """按 IP 记录连续失败次数, 超过上限锁定一段时间"""

    def __init__(self):
        self._failures: Dict[str, Tuple[int, float]] = dict()  # ip -> (连续失败次数, 最后失败时间)

    def locked_seconds(self, ip: str) -> int:
        """剩余锁定秒数, 0 表示未锁定"""
        item = self._failures.get(ip)
        if not item:
            return 0
        count, last_time = item
        remain = LOGIN_LOCK_SECONDS - (time.time() - last_time)
        if remain <= 0:
            if count >= LOGIN_MAX_FAILURES:
                self._failures.pop(ip, None)  # 锁定结束, 重新计数
            return 0
        return int(remain) + 1 if count >= LOGIN_MAX_FAILURES else 0

    def record_failure(self, ip: str) -> int:
        """记录一次失败, 返回剩余可尝试次数"""
        if len(self._failures) >= LOGIN_MAX_TRACKED_IPS:
            self._clean()
        count, last_time = self._failures.get(ip, (0, 0))
        if time.time() - last_time > LOGIN_LOCK_SECONDS:
            count = 0
        count += 1
        self._failures[ip] = (count, time.time())
        return max(LOGIN_MAX_FAILURES - count, 0)

    def record_success(self, ip: str):
        self._failures.pop(ip, None)

    def _clean(self):
        now = time.time()
        for k in [k for k, (_, t) in self._failures.items() if now - t > LOGIN_LOCK_SECONDS]:
            self._failures.pop(k, None)
