#!/usr/bin/env python3
"""像素头像素材库生成器（v0.12.1，E15）。

用算法**程序化生成**全部素材（脸型、肤色、发型、发色、眼睛、嘴、配饰、配饰颜色），写出 ``static/avatar/parts.json``；
头像 = 从各类素材里随机挑一项组合。生成是确定性的：固定种子，重跑得到逐字节相同的文件。

用法（项目 venv 即可，纯标准库）::

    python tools/avatar_lab.py                      # 重新生成 static/avatar/parts.json
    python tools/avatar_lab.py --check              # 只检查仓库里的 parts.json 与生成结果是否逐字节相同
    python tools/avatar_lab.py --sheet 120 --out .demo-data/shots/avatar-sheet.png [--seed 7] [--cols 12]
    python tools/avatar_lab.py --gallery .demo-data/shots/gallery      # 每类素材一张总览图（调试素材用）
    python tools/avatar_lab.py --diag 3000          # 随机 3000 个组合，统计各条质量规则的拒绝率与“罪魁”素材

素材的画法（全部是“带参数的形状函数 + 随机扰动 + 左右对称 + 自动描边 / 明暗着色”）：

* 脸型：按行宽度剖面（圆顶 + 下颌曲线）参数化，8 个以上的族；眼、口位置对所有脸型相同，所以任意组合都对得上；
* 发型：按“列区间”剖面（每列从发顶到发梢的区间）描述，左半边生成后镜像，再加少量不对称的发梢；
  front 层是头顶与刘海，back 层是脑后与两侧的长发；描边与明暗由统一的算法按“头发 + 脸”的整体轮廓自动算出；
* 眼睛 / 嘴：小型参数化模板（形状函数 + 间距 / 高光 / 腮红 / 眉毛的变体）；
* 配饰：形状函数组合（帽子、耳朵、角、头带、眼镜、胡子……）+ 自动描边 / 着色；
* 调色板：基色表 + 明暗斜坡（暗部偏紫、亮部偏黄）。

渲染（``compose``）、质量过滤（``random_description``）、名册挑选（``pick_roster``）在 ``app/avatars.py``，本脚本复用并重新导出。
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import struct
import sys
import zlib
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import avatars as av  # noqa: E402
from app.avatars import (  # noqa: E402,F401  (重新导出：lead 约定这些函数在本脚本里)
    check_description,
    compose,
    pick_roster,
    random_description,
)

W = H = SIZE = 20
GEN_SEED = 20260617
PARTS_PATH = ROOT / "static" / "avatar" / "parts.json"
Pt = tuple[int, int]
PxSet = set[Pt]
PxMap = dict[Pt, str]

# ---------------------------------------------------------------------------------------------------------------
# 确定性随机数：splitmix64（不依赖 Python 的 random 实现，跨平台 / 跨版本逐位一致）
# ---------------------------------------------------------------------------------------------------------------
_MASK = (1 << 64) - 1


class Rng:
    """小而稳的伪随机数发生器；接口是 ``random.Random`` 的子集（random / randrange / choice / shuffle / sample）。"""

    def __init__(self, seed: int | str = 0) -> None:
        if isinstance(seed, str):
            seed = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")
        self.state = int(seed) & _MASK

    def _next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        return z ^ (z >> 31)

    def random(self) -> float:
        return (self._next() >> 11) / float(1 << 53)

    def randrange(self, n: int) -> int:
        return self._next() % n

    def randint(self, a: int, b: int) -> int:
        return a + self._next() % (b - a + 1)

    def chance(self, p: float) -> bool:
        return self.random() < p

    def choice(self, seq):
        return seq[self._next() % len(seq)]

    def shuffle(self, items: list) -> None:
        for i in range(len(items) - 1, 0, -1):
            j = self._next() % (i + 1)
            items[i], items[j] = items[j], items[i]

    def sample(self, seq, k: int) -> list:
        pool = list(seq)
        self.shuffle(pool)
        return pool[:k]


def part_rng(kind: str, index: int) -> Rng:
    return Rng(f"{GEN_SEED}/{kind}/{index}")


# ---------------------------------------------------------------------------------------------------------------
# 颜色：基色 -> (基色, 暗部, 亮部) 斜坡
# ---------------------------------------------------------------------------------------------------------------


def _to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _to_hex(rgb: Iterable[float]) -> str:
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _hue_toward(h: float, target: float, amount: float) -> float:
    d = ((target - h + 0.5) % 1.0) - 0.5
    if abs(d) <= amount:
        return target % 1.0
    return (h + (amount if d > 0 else -amount)) % 1.0


def ramp(base: str, *, kind: str) -> dict[str, str]:
    """基色 -> {b, s, l}：暗部偏紫、饱和度略升、明度降；亮部偏黄、饱和度略降、明度升。"""
    r, g, b = (c / 255.0 for c in _to_rgb(base))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if kind == "skin":
        sh = (_hue_toward(h, 350 / 360, 0.022), min(1.0, s + 0.07), v * 0.84)
        lt = (_hue_toward(h, 45 / 360, 0.018), s * 0.82, min(1.0, v + 0.06))
    elif kind == "hair":
        sh = (_hue_toward(h, 275 / 360, 0.03), min(1.0, s + 0.06), v * 0.76)
        lt = (_hue_toward(h, 50 / 360, 0.022), s * 0.80, min(1.0, v * 1.12 + 0.07))
    else:  # accessory
        sh = (_hue_toward(h, 275 / 360, 0.028), min(1.0, s + 0.05), v * 0.73)
        lt = (_hue_toward(h, 50 / 360, 0.02), s * 0.80, min(1.0, v * 1.10 + 0.07))
    out = {"b": _to_hex((r * 255, g * 255, b * 255))}
    for key, (hh, ss, vv) in (("s", sh), ("l", lt)):
        rr, gg, bb = colorsys.hsv_to_rgb(hh, ss, vv)
        out[key] = _to_hex((rr * 255, gg * 255, bb * 255))
    return out


#: 肤色基色：浅 -> 深，再加奇幻色
SKIN_BASES = [
    "#ffe7da", "#fcd7c2", "#f6c6a3", "#efb48b", "#e3a070", "#d08a5a", "#bb7448", "#a2603a", "#8a5430",
    "#f4b8b0", "#c9a468",  # 玫瑰色、橄榄色
    "#b9bfcc", "#93c770", "#8fd3b0", "#86c5f0", "#b693e6", "#e888b8", "#e86e62", "#5fc7bd", "#f0d060",
]  # fmt: skip
#: 发色基色：自然色 + 鲜艳色
HAIR_BASES = [
    "#3a3347", "#4e3426", "#6b4430", "#8f5a35", "#a8452d", "#d8782c", "#eeb94a", "#f6e19a", "#aeb0c2", "#f2f0fa",
    "#ff7eb4", "#d9348a", "#e14040", "#ff9b3d", "#ffe24d", "#9edc4c", "#3fbf6b", "#38c9b4", "#4ccdf0", "#4b7cf2",
    "#2f3f94", "#8b52d8", "#c3a6f5", "#6f7f9a",
]  # fmt: skip
#: 配饰基色
ACCESSORY_BASES = [
    "#e04848", "#f08a30", "#f2c84b", "#3fae5a", "#33b9b0", "#4db8f0", "#3f6fe0", "#35448f", "#8a55d0", "#ff7fb0",
    "#f2f0f8", "#b9bfcc", "#4a4458", "#8a5a3a",
]  # fmt: skip
FIXED = {
    "outline": "#2a1b3d",
    "blush": "#ff8fa3",
    "eye_white": "#ffffff",
    "pupil": "#24243e",
    "mouth": "#8a2b3b",
    "tooth": "#ffffff",
}


def build_palettes() -> dict[str, Any]:
    return {
        "skin": {f"s{i:02d}": ramp(c, kind="skin") for i, c in enumerate(SKIN_BASES)},
        "hair_color": {f"c{i:02d}": ramp(c, kind="hair") for i, c in enumerate(HAIR_BASES)},
        "accessory_color": {f"k{i:02d}": ramp(c, kind="acc") for i, c in enumerate(ACCESSORY_BASES)},
        "fixed": dict(FIXED),
    }


# ---------------------------------------------------------------------------------------------------------------
# 几何：像素集合（x 向右，y 向下；画布 20×20，左右对称轴在 x = 9.5）
# ---------------------------------------------------------------------------------------------------------------


def mx(x: int) -> int:
    """x 关于对称轴的镜像。"""
    return W - 1 - x


def mirror(s: Iterable[Pt]) -> PxSet:
    return {(mx(x), y) for x, y in s}


def sym(s: Iterable[Pt]) -> PxSet:
    s = set(s)
    return s | mirror(s)


def shift(s: Iterable[Pt], dx: int, dy: int) -> PxSet:
    return {(x + dx, y + dy) for x, y in s}


def clip(s: Iterable[Pt]) -> PxSet:
    return {(x, y) for x, y in s if 0 <= x < W and 0 <= y < H}


def box(x0: int, y0: int, x1: int, y1: int) -> PxSet:
    return {(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)}


def ellipse(cx: float, cy: float, rx: float, ry: float) -> PxSet:
    """像素中心落在椭圆内的像素（像素 (x, y) 的中心是 (x, y)；cx = 9.5 表示正中）。"""
    out: PxSet = set()
    for y in range(H):
        for x in range(W):
            if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0 + 1e-9:
                out.add((x, y))
    return out


def line(x0: int, y0: int, x1: int, y1: int) -> list[Pt]:
    """Bresenham 线段（含两端）。"""
    pts = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    x, y = x0, y0
    while True:
        pts.append((x, y))
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy
    return pts


def poly(points: list[tuple[float, float]]) -> PxSet:
    """多边形填充（扫描线；像素中心 (x, y) 在多边形内才算）。"""
    out: PxSet = set()
    n = len(points)
    for y in range(H):
        xs = []
        for i in range(n):
            (x0, y0), (x1, y1) = points[i], points[(i + 1) % n]
            if (y0 <= y < y1) or (y1 <= y < y0):
                xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
        xs.sort()
        for a, b in zip(xs[0::2], xs[1::2]):
            for x in range(W):
                if a - 1e-9 <= x <= b + 1e-9:
                    out.add((x, y))
    return out


def dil4(s: Iterable[Pt]) -> PxSet:
    out = set(s)
    for x, y in list(out):
        out.update(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))
    return out


def ring(s: Iterable[Pt]) -> PxSet:
    """集合外侧一圈（4 邻接描边）。"""
    s = set(s)
    return dil4(s) - s


def rows_of(px: PxMap) -> list[str]:
    grid = [["."] * W for _ in range(H)]
    for (x, y), ch in px.items():
        if 0 <= x < W and 0 <= y < H:
            grid[y][x] = ch
    return ["".join(r) for r in grid]


def from_cols(cols: dict[int, tuple[int, int] | list[tuple[int, int]]], *, mirrored: bool = True) -> PxSet:
    """列区间剖面 -> 像素集合：``cols[x] = (y0, y1)`` 或 ``[(y0, y1), …]``（含两端）；mirrored 时按 x -> 19 - x 镜像。"""
    out: PxSet = set()
    for x, spans in cols.items():
        if isinstance(spans, tuple):
            spans = [spans]
        for y0, y1 in spans:
            for y in range(y0, y1 + 1):
                out.add((x, y))
                if mirrored:
                    out.add((mx(x), y))
    return clip(out)


# ---------------------------------------------------------------------------------------------------------------
# 脸型：行宽度剖面（圆顶 + 下颌曲线），左右对称；眼口位置对所有脸型相同
# ---------------------------------------------------------------------------------------------------------------

#: 眼睛三行（10..12）、嘴三行（14..16）、脸顶 5 行起，所有脸型共用
EYE_ROW_BOTTOM = 12
MOUTH_ROW_TOP = 14


def _sqrt01(v: float) -> float:
    return max(0.0, v) ** 0.5


def face_fill(
    *,
    top: int,
    bot: int,
    cmax: int,
    ctop: int,
    cbot: int,
    ywide: int,
    jaw: str = "round",
    dome: str = "round",
    ears: tuple[int, int, int] | None = None,
    jowl: int = 0,
) -> PxSet:
    """脸的填充区域。每行左半边宽 c 个像素（整行 2c 宽），顶行宽 2*ctop，最宽 2*cmax（在 ywide 行），下巴宽 2*cbot。

    ``jaw``：round 圆下颌 / linear 直线收窄（尖下巴）/ square 方下颌 / soft 缓收；``dome``：round / linear（上窄下宽）。
    ``ears``：(起始行, 结束行, 外凸像素数)，在这些行多出耳朵。
    """
    widths: dict[int, int] = {}
    for y in range(top, bot + 1):
        if y <= ywide:
            t = (y - top) / max(1, ywide - top)
            f = _sqrt01(1.0 - (1.0 - t) * (1.0 - t)) if dome == "round" else t
            c = ctop + (cmax - ctop) * f
        else:
            u = (y - ywide) / max(1, bot - ywide)
            if jaw == "round":
                g = _sqrt01(1.0 - u * u)
            elif jaw == "linear":
                g = 1.0 - u
            elif jaw == "square":
                g = 1.0 if u < 0.55 else _sqrt01(1.0 - ((u - 0.55) / 0.45) ** 2)
            else:  # soft
                g = 1.0 - u * u * 0.9
            c = cbot + (cmax - cbot) * g
        widths[y] = max(1, int(c + 0.5 + 1e-6))
    if jowl:
        for y in range(ywide, bot + 1):
            widths[y] = min(cmax, widths[y] + jowl)
    # 保证单调：上半部不收窄，下半部不变宽
    for y in range(top + 1, ywide + 1):
        widths[y] = max(widths[y], widths[y - 1])
    for y in range(ywide + 1, bot + 1):
        widths[y] = min(widths[y], widths[y - 1])
    if ears:
        y0, y1, extra = ears
        for y in range(y0, y1 + 1):
            widths[y] = widths[y] + extra
    fill: PxSet = set()
    for y, c in widths.items():
        for x in range(10 - c, 10 + c):
            fill.add((x, y))
    return clip(fill)


FACE_SPECS: list[tuple[str, dict[str, Any]]] = [
    ("round", dict(top=5, bot=16, cmax=7, ctop=4, cbot=4, ywide=9)),
    ("square", dict(top=5, bot=16, cmax=7, ctop=5, cbot=6, ywide=8, jaw="square")),
    ("narrow", dict(top=5, bot=16, cmax=6, ctop=4, cbot=3, ywide=9)),
    ("wide", dict(top=5, bot=16, cmax=8, ctop=5, cbot=5, ywide=10)),
    ("pointed", dict(top=5, bot=17, cmax=7, ctop=4, cbot=1, ywide=8, jaw="linear")),
    ("trapezoid", dict(top=5, bot=16, cmax=7, ctop=5, cbot=6, ywide=15, dome="linear", jaw="square")),
    ("heart", dict(top=5, bot=17, cmax=7, ctop=4, cbot=1, ywide=7, jaw="linear")),
    ("small", dict(top=6, bot=16, cmax=6, ctop=3, cbot=3, ywide=10)),
    ("robot", dict(top=5, bot=16, cmax=7, ctop=6, cbot=7, ywide=7, jaw="square")),
    ("chubby", dict(top=6, bot=16, cmax=8, ctop=5, cbot=4, ywide=11)),
    ("ears", dict(top=5, bot=16, cmax=6, ctop=4, cbot=3, ywide=9, ears=(10, 12, 1))),
    ("elf", dict(top=5, bot=17, cmax=6, ctop=4, cbot=2, ywide=9, jaw="soft", ears=(8, 10, 2))),
    ("tall", dict(top=5, bot=17, cmax=6, ctop=4, cbot=4, ywide=10, jaw="soft")),
    ("jaw", dict(top=5, bot=16, cmax=7, ctop=5, cbot=5, ywide=9, jaw="soft", jowl=1)),
]


def shade_face(fill: PxSet, *, top: int) -> PxMap:
    px: PxMap = {}
    for x, y in fill:
        ch = "b"
        if (x, y + 1) not in fill:
            ch = "s"
        elif (x + 1, y) not in fill and x >= 10 and y >= top + 5:
            ch = "s"
        px[(x, y)] = ch
    # 高光：左上额头与左颊各一小片
    for x, y in sorted(fill):
        if y == top + 1 and (x - 1, y) not in fill:
            px[(x, y)] = "l"
            if (x + 1, y) in fill:
                px[(x + 1, y)] = "l"
    return px


def build_face(name: str, spec: dict[str, Any], index: int) -> dict[str, Any]:
    fill = face_fill(**spec)
    px = shade_face(fill, top=spec["top"])
    for p in ring(fill):
        px[p] = "o"
    if name == "robot":  # 机器人脸：额头一条接缝 + 两侧螺丝
        y = spec["top"] + 2
        for x in range(4, 16):
            if (x, y) in fill and px[(x, y)] != "o":
                px[(x, y)] = "s"
        for x in (4, 15):
            if (x, spec["top"] + 5) in fill:
                px[(x, spec["top"] + 5)] = "l"
    return {"front": rows_of(px)}, fill


def build_faces() -> tuple[dict[str, Any], list[PxSet]]:
    faces: dict[str, Any] = {}
    fills = []
    for i, (name, spec) in enumerate(FACE_SPECS):
        entry, fill = build_face(name, spec, i)
        faces[f"f{i:02d}"] = entry
        fills.append(fill)
    return faces, fills
