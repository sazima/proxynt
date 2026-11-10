import math
import random
import secrets
import time
from typing import Dict, List, Tuple

"""
登录验证码 (纯标准库生成 SVG) 与按 IP 的登录失败锁定
只在 tornado 线程中使用, 不加锁

字形以笔画折线 (<path>) 绘制, SVG 源码中不出现明文字符;
每次渲染对每个字符做随机抖动/旋转/缩放/斜切/正弦扭曲, 使同一字符每次坐标都不同,
无法靠模板匹配直接还原, 需渲染成图再 OCR
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

# 笔画字形表: 每个字符 = 若干条折线, 每条折线是网格点序列
# 网格: x ∈ [0, 2] (宽 3 列), y ∈ [0, 4] (高 5 行), 左上为 (0, 0)
_GLYPHS: Dict[str, List[List[Tuple[float, float]]]] = {
    'A': [[(0, 4), (1, 0), (2, 4)], [(0.5, 2), (1.5, 2)]],
    'B': [[(0, 0), (0, 4)], [(0, 0), (1.5, 0.5), (0, 2)], [(0, 2), (1.6, 2.5), (0, 4)]],
    'C': [[(2, 0.6), (1, 0), (0, 1), (0, 3), (1, 4), (2, 3.4)]],
    'D': [[(0, 0), (0, 4)], [(0, 0), (1.6, 1), (1.6, 3), (0, 4)]],
    'E': [[(2, 0), (0, 0), (0, 4), (2, 4)], [(0, 2), (1.4, 2)]],
    'F': [[(2, 0), (0, 0), (0, 4)], [(0, 2), (1.4, 2)]],
    'G': [[(2, 0.6), (1, 0), (0, 1), (0, 3), (1, 4), (2, 3), (2, 2.2), (1.2, 2.2)]],
    'H': [[(0, 0), (0, 4)], [(2, 0), (2, 4)], [(0, 2), (2, 2)]],
    'J': [[(2, 0), (2, 3), (1, 4), (0, 3)]],
    'K': [[(0, 0), (0, 4)], [(2, 0), (0, 2), (2, 4)]],
    'L': [[(0, 0), (0, 4), (2, 4)]],
    'M': [[(0, 4), (0, 0), (1, 2), (2, 0), (2, 4)]],
    'N': [[(0, 4), (0, 0), (2, 4), (2, 0)]],
    'P': [[(0, 4), (0, 0), (1.6, 0.5), (1.6, 1.5), (0, 2)]],
    'Q': [[(1, 0), (0, 1), (0, 3), (1, 4), (2, 3), (2, 1), (1, 0)], [(1.2, 3), (2, 4)]],
    'R': [[(0, 4), (0, 0), (1.6, 0.5), (1.6, 1.5), (0, 2)], [(0.8, 2), (2, 4)]],
    'S': [[(2, 0.6), (1, 0), (0, 1), (1, 2), (2, 3), (1, 4), (0, 3.4)]],
    'T': [[(0, 0), (2, 0)], [(1, 0), (1, 4)]],
    'U': [[(0, 0), (0, 3), (1, 4), (2, 3), (2, 0)]],
    'V': [[(0, 0), (1, 4), (2, 0)]],
    'W': [[(0, 0), (0.5, 4), (1, 1.5), (1.5, 4), (2, 0)]],
    'X': [[(0, 0), (2, 4)], [(2, 0), (0, 4)]],
    'Y': [[(0, 0), (1, 2), (2, 0)], [(1, 2), (1, 4)]],
    'Z': [[(0, 0), (2, 0), (0, 4), (2, 4)]],
    '2': [[(0, 0.8), (1, 0), (2, 0.8), (2, 1.6), (0, 4), (2, 4)]],
    '3': [[(0, 0.4), (1, 0), (2, 1), (1, 2), (2, 3), (1, 4), (0, 3.6)]],
    '4': [[(1.6, 4), (1.6, 0), (0, 2.6), (2, 2.6)]],
    '5': [[(2, 0), (0, 0), (0, 1.8), (1.2, 1.6), (2, 2.6), (1, 4), (0, 3.4)]],
    '6': [[(1.8, 0.4), (0.8, 0), (0, 1.6), (0, 3), (1, 4), (2, 3), (1.2, 2), (0, 2.6)]],
    '7': [[(0, 0), (2, 0), (0.8, 4)]],
    '8': [[(1, 2), (0, 1), (1, 0), (2, 1), (1, 2), (0, 3), (1, 4), (2, 3), (1, 2)]],
    '9': [[(0.2, 3.6), (1.2, 4), (2, 2.4), (2, 1), (1, 0), (0, 1), (0.8, 2), (2, 1.4)]],
}


def _rand_color(low: int, high: int) -> str:
    return '#%02x%02x%02x' % (random.randint(low, high), random.randint(low, high), random.randint(low, high))


def _glyph_path(ch: str, cx: float, cy: float, cw: float, chh: float) -> str:
    """把一个字符的笔画折线映射到画布, 并做随机扭曲, 返回一条或多条 <path>"""
    strokes = _GLYPHS.get(ch)
    if not strokes:
        return ''
    # 每个字符一套随机变换参数
    angle = math.radians(random.uniform(-22, 22))
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    shear = random.uniform(-0.28, 0.28)           # 斜切
    sx = random.uniform(0.85, 1.1)                 # 横向缩放
    sy = random.uniform(0.9, 1.12)                 # 纵向缩放
    warp_amp = random.uniform(1.5, 3.5)            # 正弦扭曲幅度
    warp_freq = random.uniform(0.5, 1.1)
    warp_phase = random.uniform(0, math.tau)
    stroke_w = random.uniform(2.0, 3.0)
    color = _rand_color(20, 120)

    def map_point(gx: float, gy: float) -> Tuple[float, float]:
        # 归一化到以字符中心为原点: x ∈ [-1, 1], y ∈ [-1, 1]
        nx = (gx / 2.0 - 0.5) * 2.0
        ny = (gy / 4.0 - 0.5) * 2.0
        # 缩放 + 斜切
        px = nx * sx + ny * shear
        py = ny * sy
        # 旋转
        rx = px * cos_a - py * sin_a
        ry = px * sin_a + py * cos_a
        # 落到字符单元 (半宽半高)
        x = cx + rx * cw / 2.0
        y = cy + ry * chh / 2.0
        # 正弦扭曲 + 抖动
        x += math.sin(y * warp_freq * 0.2 + warp_phase) * warp_amp + random.uniform(-1.0, 1.0)
        y += math.sin(x * warp_freq * 0.2 + warp_phase) * warp_amp * 0.6 + random.uniform(-1.0, 1.0)
        return x, y

    out = []
    for stroke in strokes:
        pts = [map_point(gx, gy) for gx, gy in stroke]
        d = 'M' + ' L'.join(f'{x:.1f} {y:.1f}' for x, y in pts)
        out.append(f'<path d="{d}" stroke="{color}" stroke-width="{stroke_w:.1f}" '
                   f'fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
    return ''.join(out)


def _render_svg(code: str) -> str:
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{CAPTCHA_WIDTH}" height="{CAPTCHA_HEIGHT}" '
             f'viewBox="0 0 {CAPTCHA_WIDTH} {CAPTCHA_HEIGHT}">',
             f'<rect width="100%" height="100%" fill="{_rand_color(225, 250)}"/>']
    # 噪点
    for _ in range(30):
        parts.append(f'<circle cx="{random.uniform(0, CAPTCHA_WIDTH):.1f}" cy="{random.uniform(0, CAPTCHA_HEIGHT):.1f}" '
                     f'r="{random.uniform(0.5, 1.5):.1f}" fill="{_rand_color(100, 200)}"/>')
    # 字符: 以笔画 <path> 绘制 (源码无明文), 每个字符随机位置/字号
    step = CAPTCHA_WIDTH / (CAPTCHA_LENGTH + 1)
    for i, ch in enumerate(code):
        cx = step * (i + 1) + random.uniform(-4, 4)
        cy = CAPTCHA_HEIGHT * 0.5 + random.uniform(-3, 3)
        cw = random.uniform(16, 22)
        chh = random.uniform(26, 32)
        parts.append(_glyph_path(ch, cx, cy, cw, chh))
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
        if not captcha_id:
            return False
        item = self._store.pop(captcha_id, None)  # 不管对错, 验证一次即删除
        if item is None or not answer:
            return False
        code, create_time = item
        if time.time() - create_time > CAPTCHA_EXPIRE_SECONDS:
            return False
        return secrets.compare_digest(code, answer.strip().upper())

    def discard(self, captcha_id: str):
        """删除指定验证码 (生成新码前清掉同一用户的旧码)"""
        if captcha_id:
            self._store.pop(captcha_id, None)

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
