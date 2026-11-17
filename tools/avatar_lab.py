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
import math
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
            u, v = (x - cx) / rx, (y - cy) / ry
            if u * u + v * v <= 1.0 + 1e-9:
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
MOUTH_ROW_BOTTOM = 15


def _sqrt01(v: float) -> float:
    return math.sqrt(max(0.0, v))


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
                g = 1.0 if u < 0.55 else _sqrt01(1.0 - ((u - 0.55) / 0.45) * ((u - 0.55) / 0.45))
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


#: 脸型族（标准脸宽 14：x = 3..16）。顶行一律从第 5 行起（第 4 行是顶部描边，被头发 / 帽子盖住），
#: 下巴描边落在第 17 行（尖下巴、长脸到第 18 行）；眼口位置对所有脸型相同
FACE_SPECS: list[tuple[str, dict[str, Any]]] = [
    ("round", dict(top=5, bot=16, cmax=6, ctop=3, cbot=3, ywide=9)),
    ("round_big", dict(top=5, bot=16, cmax=7, ctop=4, cbot=4, ywide=10)),
    ("square", dict(top=5, bot=16, cmax=6, ctop=5, cbot=5, ywide=8, jaw="square")),
    ("narrow", dict(top=5, bot=16, cmax=5, ctop=3, cbot=3, ywide=9)),
    ("chubby", dict(top=6, bot=16, cmax=8, ctop=5, cbot=4, ywide=11)),
    ("pointed", dict(top=5, bot=17, cmax=6, ctop=3, cbot=1, ywide=8, jaw="linear")),
    ("trapezoid", dict(top=5, bot=16, cmax=6, ctop=4, cbot=5, ywide=15, dome="linear", jaw="square")),
    ("heart", dict(top=5, bot=17, cmax=6, ctop=4, cbot=1, ywide=7, jaw="linear")),
    ("small", dict(top=6, bot=16, cmax=5, ctop=3, cbot=3, ywide=10)),
    ("robot", dict(top=5, bot=16, cmax=6, ctop=5, cbot=6, ywide=7, jaw="square")),
    ("ears", dict(top=5, bot=16, cmax=5, ctop=3, cbot=3, ywide=9, ears=(10, 12, 1))),
    ("elf", dict(top=5, bot=17, cmax=5, ctop=3, cbot=2, ywide=9, jaw="soft", ears=(8, 10, 2))),
    ("tall", dict(top=5, bot=17, cmax=5, ctop=3, cbot=4, ywide=10, jaw="soft")),
    ("jaw", dict(top=5, bot=16, cmax=6, ctop=4, cbot=4, ywide=9, jaw="soft", jowl=1)),
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


# ---------------------------------------------------------------------------------------------------------------
# 眼睛：小型参数化模板（形状 + 间距 + 高光 + 腮红 + 眉毛）。眼睛都在第 10..12 行、左眼外缘 x ∈ [5, 7]，眼镜的镜片按这个区域画。
# 模板里：p 瞳孔、w 眼白 / 高光、x 粉色（腮红色）、m 红色（嘴的颜色）、s 肤色暗部（眼睑 / 眼袋；眼睛层的 b/s/l 取肤色）
# ---------------------------------------------------------------------------------------------------------------

#: 每个眼睛：name、rows（左眼模板，最外侧在最左、底行在第 12 行）、mode（same 右眼同形平移 / mirror 左右翻转）、xo（左眼外缘 x）、
#: right（可选，右眼另给模板，用于眨眼）、brow（眉毛类型）、blush（下方加腮红）、dy（整体上移行数）
EYE_SPECS: list[dict[str, Any]] = [
    dict(name="bead", rows=["wp", "pp"], xo=6),
    dict(name="bead_shy", rows=["wp", "pp"], xo=5, blush=True),
    dict(name="tall", rows=["wp", "pp", "pp"], xo=6),
    dict(name="tall_shy", rows=["wp", "pp", "pp"], xo=5, blush=True),
    dict(name="tall_lash", rows=["pwp", ".pp", ".pp"], xo=5, mode="mirror"),
    dict(name="big", rows=["wpp", "ppp", "ppw"], xo=5),
    dict(name="big_shy", rows=["wpp", "ppp", "ppw"], xo=5, blush=True),
    dict(name="sparkle", rows=["wpw", "ppp", "pwp"], xo=5),
    dict(name="dot", rows=["p", "p"], xo=7),
    dict(name="dot_wide", rows=["p", "p"], xo=6),
    dict(name="dot_flat_brow", rows=["p", "p"], xo=6, brow="flat"),
    dict(name="dot_worry", rows=["p", "p"], xo=6, brow="worry"),
    dict(name="happy", rows=[".p.", "p.p"], xo=5),
    dict(name="happy_shy", rows=[".p.", "p.p"], xo=5, blush=True),
    dict(name="closed", rows=["ppp"], xo=5),
    dict(name="sad", rows=["p.p", ".p."], xo=5),
    dict(name="sleepy", rows=["ppp", "pwp"], xo=5),
    dict(name="angry", rows=["wp", "pp"], xo=6, brow="angry"),
    dict(name="worried", rows=["wp", "pp"], xo=6, brow="worry"),
    dict(name="wink", rows=["wp", "pp"], right=[".p.", "p.p"], xo=6),
    dict(name="wink_l", rows=[".p.", "p.p"], right=["wp", "pp"], xo=5),
    dict(name="cross", rows=["p.p", ".p.", "p.p"], xo=5),
    dict(name="heart", rows=["x.x", "xxx", ".x."], xo=5),
    dict(name="ring", rows=["ppp", "pwp", "ppp"], xo=5),
    dict(name="slit", rows=["wpw", "wpw"], xo=5),
    dict(name="glance_r", rows=["wp", "wp"], xo=6),
    dict(name="glance_l", rows=["pw", "pw"], xo=6),
    dict(name="red", rows=["wm", "mm"], xo=6),
    dict(name="pink", rows=["wx", "xx"], xo=6),
    dict(name="led", rows=["pp", "pp"], xo=6),
    dict(name="tired", rows=["wp", "pp", "ss"], xo=6, mode="mirror"),
    dict(name="droopy", rows=["ss", "wp", "pp"], xo=6),
]
_BROWS = {  # 左眉（外侧在左）：相对“眼睛左上角”的 (dx, dy)
    "flat": [(0, -2), (1, -2)],
    "angry": [(0, -3), (1, -2), (2, -2)],
    "worry": [(0, -2), (1, -3), (2, -3)],
}


def build_eye(spec: dict[str, Any]) -> dict[str, Any]:
    px: PxMap = {}
    mode = spec.get("mode", "same")
    xo = spec["xo"]

    def place(rows: list[str], x_left: int, flip: bool) -> tuple[int, int]:
        h, w = len(rows), len(rows[0])
        top = EYE_ROW_BOTTOM - spec.get("dy", 0) - (h - 1)
        for j, row in enumerate(rows):
            for i, ch in enumerate(row):
                if ch != ".":
                    px[(x_left + (w - 1 - i if flip else i), top + j)] = ch
        return top, w

    top, w = place(spec["rows"], xo, False)
    right = spec.get("right")
    if right is not None:
        place(right, mx(xo + len(right[0]) - 1), False)
    elif mode == "mirror":
        place(spec["rows"], mx(xo + w - 1), True)
    else:
        place(spec["rows"], mx(xo + w - 1), False)
    brow = spec.get("brow")
    if brow:
        for dx, dy in _BROWS[brow]:
            px[(xo + dx, top + dy)] = "p"
            px[(mx(xo + dx), top + dy)] = "p"
    if spec.get("blush"):
        for x in (xo - 1, xo):
            px[(x, EYE_ROW_BOTTOM + 1)] = "x"
            px[(mx(x), EYE_ROW_BOTTOM + 1)] = "x"
    return {"front": rows_of(px)}


def build_eyes() -> dict[str, Any]:
    return {f"e{i:02d}": build_eye(spec) for i, spec in enumerate(EYE_SPECS)}


# ---------------------------------------------------------------------------------------------------------------
# 嘴：小型参数化模板，水平居中（偶数宽），顶行在第 14 行起
# m 嘴 / 口腔、t 牙齿、x 粉色（舌头 / 嘟嘴）、p 暗色
# ---------------------------------------------------------------------------------------------------------------

MOUTH_SPECS: list[dict[str, Any]] = [
    dict(name="smile", rows=["m..m", ".mm."], dy=0),
    dict(name="smile_big", rows=["m....m", ".mmmm."], dy=0),
    dict(name="flat_s", rows=["mm"], dy=0),
    dict(name="flat", rows=["mmmm"], dy=0),
    dict(name="frown", rows=[".mm.", "m..m"], dy=0),
    dict(name="o", rows=["mm", "mm"], dy=0),
    dict(name="surprised", rows=["mm", "mm", "mm"], dy=0),
    dict(name="ring", rows=[".mm.", "mppm", ".mm."], dy=0),
    dict(name="grin", rows=["mmmmmm", ".tttt."], dy=0),
    dict(name="laugh", rows=["mmmmmm", "mttttm", ".mxxm."], dy=0),
    dict(name="open", rows=["mmmm", "mttm", ".mm."], dy=0),
    dict(name="big_open", rows=["mmmm", "mxxm", ".mm."], dy=0),
    dict(name="tongue", rows=["mmmm", ".xx.", ".xx."], dy=0),
    dict(name="smirk_r", rows=["...m", "mmm."], dy=0),
    dict(name="smirk_l", rows=["m...", ".mmm"], dy=0),
    dict(name="fangs", rows=["m..m", "tmmt"], dy=0),
    dict(name="clench", rows=["mttm"], dy=0),
    dict(name="wavy", rows=["m.m.m.", ".m.m.m"], dy=0),
    dict(name="kiss", rows=["xx"], dy=0),
    dict(name="cat", rows=["m.m.m.", ".m.m.m"], dy=0),
]


def build_mouth(spec: dict[str, Any]) -> dict[str, Any]:
    px: PxMap = {}
    rows = spec["rows"]
    w = len(rows[0])
    x0 = 10 - w // 2
    top = MOUTH_ROW_BOTTOM - (len(rows) - 1) - spec.get("dy", 0)  # 嘴的底行对齐第 15 行
    for j, row in enumerate(rows):
        for i, ch in enumerate(row):
            if ch != ".":
                px[(x0 + i, top + j)] = ch
    return {"front": rows_of(px)}


def build_mouths() -> dict[str, Any]:
    return {f"m{i:02d}": build_mouth(spec) for i, spec in enumerate(MOUTH_SPECS)}


# ---------------------------------------------------------------------------------------------------------------
# 样张：纯标准库写 PNG（没有 Pillow 也能用），内置 3×5 小字体画编号
# ---------------------------------------------------------------------------------------------------------------

_FONT = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111",
    "4": "101101111001001", "5": "111100111001111", "6": "111100111101111", "7": "111001001001001",
    "8": "111101111101111", "9": "111101111001111", "#": "101111101111101", "-": "000000111000000",
    ".": "000000000000010", ":": "000010000010000", "/": "001001010100100", " ": "000000000000000",
}  # fmt: skip


class Image:
    """RGBA 画布（bytearray），只做样张需要的几种操作。"""

    def __init__(self, width: int, height: int, bg: tuple[int, int, int] = (255, 255, 255)) -> None:
        self.w, self.h = width, height
        self.buf = bytearray(bytes((*bg, 255)) * (width * height))

    def rect(self, x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(self.w, x1), min(self.h, y1)
        row = bytes((*color, 255)) * max(0, x1 - x0)
        for y in range(y0, y1):
            self.buf[(y * self.w + x0) * 4 : (y * self.w + x1) * 4] = row

    def sprite(self, pixels, x0: int, y0: int, scale: int) -> None:
        for r, row in enumerate(pixels):
            for c, (red, green, blue, alpha) in enumerate(row):
                if alpha:
                    self.rect(x0 + c * scale, y0 + r * scale, x0 + (c + 1) * scale, y0 + (r + 1) * scale, (red, green, blue))

    def text(self, x: int, y: int, text: str, color: tuple[int, int, int], scale: int = 2) -> None:
        for ch in text:
            glyph = _FONT.get(ch, _FONT[" "])
            for i, bit in enumerate(glyph):
                if bit == "1":
                    gx, gy = i % 3, i // 3
                    self.rect(x + gx * scale, y + gy * scale, x + (gx + 1) * scale, y + (gy + 1) * scale, color)
            x += 4 * scale

    def png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(self.buf[y * self.w * 4 : (y + 1) * self.w * 4]) for y in range(self.h))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b"")
        )


def render_sheet(
    parts: dict[str, Any],
    descs: list[dict[str, Any]],
    *,
    scale: int = 6,
    cols: int = 12,
    dark: bool = False,
    labels: list[str] | None = None,
) -> Image:
    """把一批描述排成带网格、带编号的样张（最近邻放大 ``scale`` 倍）。"""
    pad = 6
    cell_w = SIZE * scale + pad * 2
    cell_h = SIZE * scale + pad * 2 + 12
    rows = (len(descs) + cols - 1) // cols
    bg = (34, 30, 46) if dark else (236, 233, 242)
    grid = (58, 52, 76) if dark else (205, 200, 218)
    ink = (200, 196, 220) if dark else (90, 82, 112)
    img = Image(cols * cell_w + 1, rows * cell_h + 1, bg)
    for c in range(cols + 1):
        img.rect(c * cell_w, 0, c * cell_w + 1, rows * cell_h + 1, grid)
    for r in range(rows + 1):
        img.rect(0, r * cell_h, cols * cell_w + 1, r * cell_h + 1, grid)
    for i, desc in enumerate(descs):
        r, c = divmod(i, cols)
        x0, y0 = c * cell_w + 1, r * cell_h + 1
        img.sprite(compose(parts, desc), x0 + pad, y0 + 2, scale)
        label = labels[i] if labels else f"{i + 1:03d}"
        img.text(x0 + 3, y0 + SIZE * scale + 5, label, ink, 2)
    return img


# ---------------------------------------------------------------------------------------------------------------
# 发型引擎：列区间剖面（每列从发顶到发梢）+ 椭圆冠 + 刘海图样 + 尖刺 / 卷边 / 发束；左半边生成后镜像，再加少量不对称的发梢。
# front 层 = 头顶与刘海（盖在脸上），back 层 = 脑后与两侧的长发（垫在脸后面）；
# 描边与明暗由 finish_hair 按“头发整体轮廓”统一算出：对着透明背景的一圈画描边，贴着脸（所有脸型的公共区域）的一侧不描。
# ---------------------------------------------------------------------------------------------------------------


def crown_tops(top_row: int, ry: float, rx: float, square: bool = False) -> dict[int, int]:
    """发冠的上沿：每列最高的发根行。椭圆（square=True 时是更方的超椭圆，用于平头 / 方顶）。"""
    cy = top_row - 0.5 + ry
    tops: dict[int, int] = {}
    for x in range(W):
        dx = (x - 9.5) / rx
        if abs(dx) < 1.0:
            if square:
                r = math.sqrt(math.sqrt(max(0.0, 1.0 - (dx * dx) * (dx * dx))))
            else:
                r = math.sqrt(1.0 - dx * dx)
            tops[x] = int(cy - ry * r + 0.5)
    return tops


def bang_pattern(kind: str, base: int, n: int, rng: Rng) -> list[int]:
    """刘海的下沿行，从最外侧的刘海列排到正中那一列（共 n 列）。"""
    if n <= 0:
        return []
    last = max(1, n - 1)
    if kind == "arch":  # 中间短、两边长
        return [base + 1 - (2 * i) // last for i in range(n)]
    if kind == "curtain":  # 中分，帘子状
        return [base + 1 - (4 * i) // last for i in range(n)]
    if kind == "vee":  # 中间长、两边短（美人尖向下）
        return [base - 1 + (2 * i) // last for i in range(n)]
    if kind == "zig":
        ph = rng.randrange(2)
        return [base + (i + ph) % 2 for i in range(n)]
    if kind == "zig3":
        ph = rng.randrange(3)
        return [base + (0, 1, 2)[(i + ph) % 3] for i in range(n)]
    if kind == "wisps":
        return [base + rng.randint(-1, 1) for _ in range(n)]
    return [base] * n  # blunt


def hair_cap(
    rng: Rng,
    *,
    top: int = 2,
    ry: float = 5.5,
    rx: float = 8.2,
    base: int = 7,
    bang: str = "blunt",
    side_len: int = 9,
    side_cols: int = 2,
    side_lens: list[int] | None = None,
    square: bool = False,
    jitter: float = 0.0,
    spikes: tuple[int, int] | None = None,
) -> PxSet:
    """头顶 + 刘海 + 两鬓（左右对称）。``base`` 是正中刘海的下沿行，``side_len`` 是鬓角垂到的行。"""
    tops = crown_tops(top, ry, rx, square)
    if spikes:
        period, amp = spikes
        pat = {2: (0, 1), 3: (0, 2, 1), 4: (0, 1, 2, 1)}[period]
        ph = rng.randrange(period)
        for x in [x for x in tops if x <= 9]:
            h = (pat[(x + ph) % period] * amp) // max(1, max(pat))
            tops[x] = max(1, tops[x] - h)
            tops[mx(x)] = tops[x]
    xs = sorted(x for x in tops if x <= 9)
    n_side = min(side_cols, len(xs))
    pat_b = bang_pattern(bang, base, len(xs) - n_side, rng)
    bottoms: dict[int, int] = {}
    for i, x in enumerate(xs):
        if i < n_side:
            bottoms[x] = side_lens[i] if side_lens and i < len(side_lens) else side_len
        else:
            bottoms[x] = pat_b[i - n_side]
    if len(xs) > n_side and side_len - base >= 3 and not side_lens:
        bottoms[xs[n_side]] = max(bottoms[xs[n_side]], (side_len + base) // 2)
    if jitter:
        for x in xs[n_side:]:
            if rng.chance(jitter):
                bottoms[x] += rng.choice((-1, 1))
    out: PxSet = set()
    for x, y0 in tops.items():
        y1 = bottoms[x if x <= 9 else mx(x)]
        for y in range(y0, max(y0, y1) + 1):
            out.add((x, y))
    return out


def strand(points: list[Pt], widths: int | list[int]) -> PxSet:
    """沿折线画一条发束：每个线上的点横向加宽到 ``widths``（可按段给）。"""
    out: PxSet = set()
    for i in range(len(points) - 1):
        (x0, y0), (x1, y1) = points[i], points[i + 1]
        w = widths[i] if isinstance(widths, list) else widths
        for x, y in line(x0, y0, x1, y1):
            for k in range(w):
                out.add((x - (w - 1) // 2 + k, y))
    return out


def scallop(shape: PxSet, xs: Iterable[int], ys: Iterable[int], r: float) -> PxSet:
    """卷边：在轮廓上按给定的列（上沿）/ 行（左右沿）加小圆，得到起伏的外缘（卷发 / 蓬松）。"""
    out = set(shape)
    for x in xs:
        for xx in {x, mx(x)}:
            col = [y for (px_, y) in shape if px_ == xx]
            if col:
                out |= ellipse(xx, min(col), r, r)
    for y in ys:
        row = [x for (x, py) in shape if py == y]
        if row:
            out |= ellipse(min(row), y, r, r)
            out |= ellipse(max(row), y, r, r)
    return out


def _inside(s: Iterable[Pt]) -> PxSet:
    """填充只能画在 [1, 18]² 内（外面一圈留给描边）。"""
    return {(x, y) for x, y in s if 1 <= x <= W - 2 and 1 <= y <= H - 2}


def _shade(px: PxMap, region: PxSet, whole: PxSet) -> None:
    """明暗：下沿（上面还有头发的）和右侧朝外 / 朝脸的边着暗部。"""
    for x, y in region:
        ch = "b"
        if (x, y + 1) not in whole and (x, y - 1) in whole:
            ch = "s"
        elif (x + 1, y) not in whole and (x - 1, y) in whole and (x, y - 1) in whole:
            ch = "s"
        px[(x, y)] = ch


def finish_hair(front: PxSet, back: PxSet, core: PxSet, rng: Rng, *, highlight: bool = True) -> tuple[PxMap | None, PxMap | None]:
    """头发的 front / back 像素 -> 带描边与明暗的字符层。"""
    front, back = _inside(front), _inside(back)
    if not front and not back:
        return None, None
    whole = front | back
    fpx: PxMap = {}
    bpx: PxMap = {}
    _shade(fpx, front, whole)
    _shade(bpx, back, whole)
    if highlight and front:  # 左上方一小段弧形高光
        tops: dict[int, int] = {}
        for x, y in front:
            if 3 <= x <= 9 and (x not in tops or y < tops[x]):
                tops[x] = y
        if tops:
            x0 = 4 + rng.randrange(2)
            for k, x in enumerate((x0, x0 + 1, x0 + 2)):
                if x in tops:
                    y = tops[x] + (1 if k < 2 else 2)
                    if (x, y) in front and (x, y + 1) in whole and fpx.get((x, y)) == "b":
                        fpx[(x, y)] = "l"
    for p in ring(whole):
        if not (0 <= p[0] < W and 0 <= p[1] < H) or p in core:
            continue
        near_front = any(n in front for n in ((p[0] + 1, p[1]), (p[0] - 1, p[1]), (p[0], p[1] + 1), (p[0], p[1] - 1)))
        (fpx if near_front else bpx)[p] = "o"
    return (fpx or None), (bpx or None)


def add_tips(front: PxSet, back: PxSet, rng: Rng, *, p: float = 0.8) -> tuple[PxSet, PxSet]:
    """左右对称之后，在发梢处加少量不对称：一侧多出 1~2 个像素的发尖，另一侧偶尔少 1 个像素。"""
    if not rng.chance(p):
        return front, back
    whole = front | back
    low = sorted((x, y) for x, y in whole if y >= 9 and (x, y + 1) not in whole and y <= H - 4)
    if not low:
        return front, back
    side_left = rng.chance(0.5)
    cand = [q for q in low if (q[0] <= 9) == side_left]
    if cand:
        x, y = cand[rng.randrange(len(cand))]
        tip = {(x, y + 1)} | ({(x + (1 if side_left else -1), y + 1)} if rng.chance(0.3) else set())
        (back if (x, y) in back and (x, y) not in front else front).update(tip)
    other = [q for q in low if (q[0] <= 9) != side_left]
    if other and rng.chance(0.4):
        x, y = other[rng.randrange(len(other))]
        front.discard((x, y))
        back.discard((x, y))
    return front, back


# ------------------------------------ 发型配方 ------------------------------------


def _cap(rng: Rng, **kw: Any) -> PxSet:
    return hair_cap(rng, **kw)


def _back_curtain(rows: tuple[int, int], x_out: int, bottoms: dict[int, int]) -> PxSet:
    """脑后的长发：x = x_out..9 的整块（左半，镜像），第 ``rows[0]`` 行起；``bottoms`` 给出外侧几列的垂到行。"""
    out: PxSet = set()
    for x in range(x_out, 10):
        y1 = bottoms.get(x, rows[1])
        for y in range(rows[0], y1 + 1):
            out.add((x, y))
            out.add((mx(x), y))
    return out


def _tails(rng: Rng, *, x0: int, y0: int, y1: int, flare: int, width: int, wave: int = 0) -> PxSet:
    """左右一对垂下的发束（双马尾 / 辫子 / 公主发）：从 (x0, y0) 垂到 y1，末端向外飘 ``flare`` 格。"""
    pts = [(x0, y0), (x0 - flare // 2, (y0 + y1) // 2), (x0 - flare, y1)]
    if wave:
        pts = [(x0, y0), (x0 - wave, y0 + (y1 - y0) // 3), (x0 + wave, y0 + 2 * (y1 - y0) // 3), (x0 - flare, y1)]
    s = strand(pts, [width] * (len(pts) - 1))
    return s | mirror(s)


def _afro(rng: Rng, *, ry: float, rx: float, cy: float) -> PxSet:
    return ellipse(9.5, cy, rx, ry)


def hair_recipes() -> list[tuple[str, Any]]:
    """(名字, 构造函数(rng) -> (front, back))。顺序固定，编号 h01 起。"""
    R: list[tuple[str, Any]] = []

    def add(name: str, fn: Any) -> None:
        R.append((name, fn))

    # ---- 短发 ----
    add("buzz", lambda r: (_cap(r, top=3, ry=4.2, rx=7.8, base=5, side_len=7), set()))
    add("crop", lambda r: (_cap(r, top=2, ry=5.2, rx=8.2, base=6, bang="wisps", side_len=8), set()))
    add("bowl", lambda r: (_cap(r, top=2, ry=5.5, rx=8.4, base=8, side_len=10), set()))
    add("sweep", lambda r: (_cap(r, top=2, ry=5.4, rx=8.4, base=7, bang="arch", side_len=9), set()))
    add("spiky", lambda r: (_cap(r, top=2, ry=5.2, rx=8.2, base=6, bang="zig", side_len=8, spikes=(3, 2)), set()))
    add("curtain", lambda r: (_cap(r, top=2, ry=5.5, rx=8.4, base=8, bang="curtain", side_len=11), set()))
    add("flat_top", lambda r: (_cap(r, top=1, ry=6.5, rx=8.4, base=6, side_len=8, square=True), set()))
    add("undercut", lambda r: (_cap(r, top=1, ry=6.2, rx=6.9, base=6, bang="zig", side_len=5, square=True), set()))
    add("messy", lambda r: (_cap(r, top=1, ry=6.0, rx=8.6, base=7, bang="wisps", side_len=9, jitter=0.4, spikes=(2, 1)), set()))
    add("pixie", lambda r: (_cap(r, top=2, ry=5.4, rx=8.4, base=7, bang="vee", side_len=10), set()))
    add("pompadour", lambda r: (_cap(r, top=1, ry=6.5, rx=7.6, base=6, bang="arch", side_len=7, square=True), set()))
    add("receding", lambda r: (_cap(r, top=2, ry=5.2, rx=8.2, base=5, bang="vee", side_len=9), set()))
    add("mushroom", lambda r: (_cap(r, top=1, ry=6.4, rx=8.8, base=9, side_len=12, side_cols=3), set()))
    # ---- 中长发 ----
    add("bob", lambda r: (_cap(r, top=2, ry=5.5, rx=8.4, base=7, bang="arch", side_len=13), _back_curtain((6, 14), 1, {1: 13, 2: 14})))
    add("bob_flip", lambda r: (_cap(r, top=2, ry=5.5, rx=8.4, base=7, side_len=12), _back_curtain((6, 14), 0, {0: 13, 1: 14, 2: 14})))
    add("wolf", lambda r: (_cap(r, top=1, ry=6.0, rx=8.6, base=7, bang="wisps", side_len=12, jitter=0.3),
                           _back_curtain((6, 16), 1, {1: 14 + r.randrange(3), 2: 15 + r.randrange(3), 3: 16})))
    add("shoulder", lambda r: (_cap(r, top=2, ry=5.5, rx=8.4, base=7, bang="arch", side_len=15), _back_curtain((6, 17), 0, {0: 15, 1: 17, 2: 17})))
    # ---- 长发 ----
    add("long", lambda r: (_cap(r, top=2, ry=5.5, rx=8.4, base=7, side_len=15), _back_curtain((6, 18), 0, {0: 17, 1: 18, 2: 18, 3: 17})))
    add("long_wave", lambda r: (_cap(r, top=2, ry=5.5, rx=8.4, base=7, bang="arch", side_len=14),
                                _back_curtain((6, 18), 0, {0: 16, 1: 18, 2: 17, 3: 18, 4: 16})))
    add("long_part", lambda r: (_cap(r, top=2, ry=5.5, rx=8.4, base=8, bang="curtain", side_len=16), _back_curtain((6, 18), 0, {0: 17, 1: 18, 2: 18})))
    add("hime", lambda r: (_cap(r, top=2, ry=5.5, rx=8.4, base=8, side_len=15, side_cols=3), _back_curtain((6, 18), 0, {0: 17, 1: 18, 2: 18})))
    add("layered", lambda r: (_cap(r, top=2, ry=5.5, rx=8.4, base=7, bang="arch", side_len=14, side_cols=3, side_lens=[16, 14, 12]),
                              _back_curtain((6, 18), 0, {0: 16, 1: 18, 2: 17})))
    # ---- 发束 / 丸子 ----
    add("pony_side", lambda r: (_cap(r, top=2, ry=5.4, rx=8.2, base=7, bang="arch", side_len=8), strand([(14, 4), (17, 8), (18, 12), (16, 16)], [3, 3, 2])))
    add("pony_high", lambda r: (_cap(r, top=3, ry=4.6, rx=8.0, base=7, side_len=8) | ellipse(14.5, 2.4, 2.4, 1.9),
                                strand([(15, 3), (18, 7), (18, 11), (17, 14)], [3, 3, 2])))
    add("twin_low", lambda r: (_cap(r, top=2, ry=5.4, rx=8.2, base=7, side_len=8), _tails(r, x0=3, y0=8, y1=17, flare=2, width=3)))
    add("twin_high", lambda r: (_cap(r, top=3, ry=4.8, rx=8.2, base=7, side_len=8) | sym(ellipse(3.0, 4.2, 1.8, 1.8)),
                                _tails(r, x0=2, y0=4, y1=16, flare=2, width=3, wave=1)))
    add("buns_twin", lambda r: (_cap(r, top=3, ry=4.7, rx=8.0, base=7, side_len=8) | sym(ellipse(4.6, 2.6, 2.3, 2.1)), set()))
    add("bun_top", lambda r: (_cap(r, top=3, ry=4.7, rx=8.0, base=7, bang="arch", side_len=8) | ellipse(9.5, 1.9, 3.0, 2.1), set()))
    add("braids", lambda r: (_cap(r, top=2, ry=5.4, rx=8.2, base=7, side_len=9), _tails(r, x0=3, y0=9, y1=18, flare=0, width=2, wave=1)))
    add("braid_one", lambda r: (_cap(r, top=2, ry=5.4, rx=8.2, base=7, bang="arch", side_len=8), strand([(15, 8), (16, 12), (15, 15), (16, 18)], [3, 2, 2])))
    add("pigtails", lambda r: (_cap(r, top=2, ry=5.4, rx=8.2, base=7, side_len=9), sym(ellipse(1.8, 10.5, 1.9, 3.0))))
    add("topknot", lambda r: (_cap(r, top=3, ry=4.6, rx=8.0, base=6, side_len=8) | ellipse(9.5, 1.6, 1.9, 1.3), set()))
    # ---- 蓬松 / 卷 ----
    add("afro", lambda r: (_afro(r, ry=6.1, rx=8.6, cy=7.2) & box(0, 0, 19, 8) | _cap(r, top=1, ry=6.0, rx=8.6, base=6, side_len=11, side_cols=3),
                           _afro(r, ry=6.1, rx=8.6, cy=7.2) & box(0, 0, 19, 12)))
    add("curly", lambda r: (scallop(_cap(r, top=3, ry=4.4, rx=7.6, base=7, side_len=10), (3, 6, 9), (6, 9), 1.7), set()))
    add("curly_long", lambda r: (scallop(_cap(r, top=3, ry=4.4, rx=7.6, base=7, side_len=12), (3, 6, 9), (6, 9, 12), 1.7),
                                 scallop(_back_curtain((6, 16), 1, {1: 15, 2: 16}), (), (8, 11, 14), 1.7)))
    add("fluffy", lambda r: (scallop(_cap(r, top=3, ry=4.4, rx=7.8, base=7, side_len=9), (2, 4, 7, 9), (7, 10), 1.9), set()))
    add("ahoge", lambda r: (_cap(r, top=3, ry=4.6, rx=8.0, base=7, bang="arch", side_len=8) | strand([(9, 3), (9, 2), (10, 1), (11, 1)], 1), set()))
    add("wild", lambda r: (_cap(r, top=3, ry=4.6, rx=8.0, base=6, bang="zig3", side_len=8)
                           | poly([(3, 4), (4, 0.5), (6, 3.5)]) | poly([(7, 3.5), (9.5, 0.5), (12, 3.5)]) | poly([(13, 3.5), (15, 0.5), (16.5, 4)]), set()))
    add("mohawk", lambda r: (box(8, 1, 11, 4) | box(7, 4, 12, 7) | box(8, 8, 11, 8), set()))
    add("dreads", lambda r: (_cap(r, top=2, ry=5.2, rx=8.2, base=6, bang="zig", side_len=8),
                             strand([(2, 8), (1, 12), (2, 16)], 2) | strand([(4, 9), (3, 13), (4, 15)], 1) | mirror(strand([(2, 8), (1, 12), (2, 16)], 2)) | mirror(strand([(4, 9), (3, 13), (4, 15)], 1))))
    add("bald_sides", lambda r: (sym(ellipse(3.0, 9.0, 2.2, 3.2)) & box(0, 6, 19, 13), set()))
    add("tufts", lambda r: (_cap(r, top=3, ry=4.4, rx=7.8, base=6, bang="zig", side_len=8) | sym(poly([(3, 4.5), (4, 0.5), (6.5, 3.5)])), set()))
    add("swirl", lambda r: (_cap(r, top=3, ry=4.6, rx=8.0, base=7, side_len=8) | poly([(9.5, 0.5), (5.5, 4.5), (13.5, 4.5)]) | ellipse(9.5, 2.5, 2.4, 2.0), set()))
    add("flicks", lambda r: (_cap(r, top=2, ry=5.2, rx=8.2, base=7, bang="zig", side_len=9), sym(poly([(2, 9), (0.5, 11), (2.5, 12)]))))
    add("sidecut", lambda r: (_cap(r, top=2, ry=5.4, rx=8.4, base=8, bang="arch", side_len=11) - box(14, 6, 19, 19), set()))
    return R


def build_hair(face_fills: list[PxSet]) -> dict[str, Any]:
    core = set.intersection(*face_fills)
    out: dict[str, Any] = {"h00": {"back": None, "front": None}}
    seen: set[tuple] = set()
    for name, fn in hair_recipes():
        rng = part_rng("hair", len(out))
        front, back = fn(rng)
        front, back = set(front), set(back)
        if name not in ("sidecut",):
            front, back = add_tips(front, back, rng)
        fpx, bpx = finish_hair(front, back, core, rng)
        rows = (tuple(rows_of(fpx)) if fpx else None, tuple(rows_of(bpx)) if bpx else None)
        if rows in seen:
            continue
        seen.add(rows)
        out[f"h{len(out):02d}"] = {"back": list(rows[1]) if rows[1] else None, "front": list(rows[0]) if rows[0] else None}
    return out


# ---------------------------------------------------------------------------------------------------------------
# 配饰（占位，稍后展开）
# ---------------------------------------------------------------------------------------------------------------


def build_accessories(face_fills: list[PxSet]) -> dict[str, Any]:
    return {"a00": {"back": None, "front": None}}


# ---------------------------------------------------------------------------------------------------------------
# 总装与输出
# ---------------------------------------------------------------------------------------------------------------


def build_parts() -> dict[str, Any]:
    """生成整个素材库（确定性）。"""
    faces, fills = build_faces()
    return {
        "version": 1,
        "size": SIZE,
        "palettes": build_palettes(),
        "parts": {
            "face": faces,
            "hair": build_hair(fills),
            "eyes": build_eyes(),
            "mouth": build_mouths(),
            "accessory": build_accessories(fills),
        },
    }


def parts_text(parts: dict[str, Any]) -> str:
    """parts.json 的文本：2 空格缩进，字符行各占一行（便于 git diff 和肉眼看素材）。"""
    return json.dumps(parts, ensure_ascii=True, indent=1) + "\n"


def write_parts(path: Path = PARTS_PATH) -> dict[str, Any]:
    parts = build_parts()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(parts_text(parts))
    return parts


def part_counts(parts: dict[str, Any]) -> dict[str, int]:
    return {
        "face": len(parts["parts"]["face"]),
        "skin": len(parts["palettes"]["skin"]),
        "hair": len(parts["parts"]["hair"]),
        "hair_color": len(parts["palettes"]["hair_color"]),
        "eyes": len(parts["parts"]["eyes"]),
        "mouth": len(parts["parts"]["mouth"]),
        "accessory": len(parts["parts"]["accessory"]),
        "accessory_color": len(parts["palettes"]["accessory_color"]),
    }


# ---------------------------------------------------------------------------------------------------------------
# 素材总览（调试用）与命令行
# ---------------------------------------------------------------------------------------------------------------

_BASE_DESC = {
    "v": 1, "face": "f00", "skin": "s03", "hair": "h00", "hair_color": "c02", "eyes": "e00", "mouth": "m00",
    "accessory": "a00", "accessory_color": "k00",
}  # fmt: skip


def gallery_descs(parts: dict[str, Any], category: str, *, base: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """某一类素材的每一项，各配一个中性的底（其余类别固定，会变化的颜色循环取）。"""
    ids = sorted(parts["parts"][category])
    base = dict(base or _BASE_DESC)
    pal = parts["palettes"]
    skins, hairs, accs = sorted(pal["skin"]), sorted(pal["hair_color"]), sorted(pal["accessory_color"])
    out = []
    for i, pid in enumerate(ids):
        d = dict(base)
        d[category] = pid
        if category == "hair":
            d["hair_color"] = hairs[(i * 5 + 1) % len(hairs)]
        elif category == "accessory":
            d["accessory_color"] = accs[(i * 3 + 1) % len(accs)]
            d["hair_color"] = hairs[(i * 7 + 2) % len(hairs)]
        else:
            d["skin"] = skins[(i * 3) % 11]
        out.append(d)
    return out


def write_png(img: Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(img.png())


def write_gallery(parts: dict[str, Any], directory: Path, *, scale: int = 5, cols: int = 10, dark: bool = False) -> list[Path]:
    out = []
    for category in ("face", "eyes", "mouth", "hair", "accessory"):
        base = dict(_BASE_DESC)
        if category in ("face", "eyes", "mouth"):
            base["eyes"] = "e00" if category != "eyes" else base["eyes"]
        if category in ("accessory", "eyes", "mouth", "face") and "h01" in parts["parts"]["hair"]:
            base["hair"] = "h01"
        descs = gallery_descs(parts, category, base=base)
        labels = [f"{i:02d}" for i in range(len(descs))]
        path = directory / f"gallery-{category}.png"
        write_png(render_sheet(parts, descs, scale=scale, cols=cols, dark=dark, labels=labels), path)
        out.append(path)
    return out


def sample_descs(parts: dict[str, Any], count: int, seed: int, *, roster: bool | None = None) -> list[dict[str, Any]]:
    rng = Rng(f"sheet/{seed}")
    if roster is None:
        roster = count <= 12
    if roster:
        return pick_roster(parts, count, rng)
    return [random_description(parts, rng) for _ in range(count)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="像素头像素材库生成器（见模块文档）")
    ap.add_argument("--out", type=Path, help="样张 PNG 的输出路径（配合 --sheet）")
    ap.add_argument("--sheet", type=int, metavar="N", help="随机组合 N 个头像，渲染成一张样张（N≤12 时按一桌名册挑选）")
    ap.add_argument("--independent", action="store_true", help="--sheet 时每个头像独立随机（不做名册的两两区分）")
    ap.add_argument("--seed", type=int, default=7, help="样张 / 诊断的随机种子（默认 7）")
    ap.add_argument("--cols", type=int, default=0, help="样张列数（默认按数量自动）")
    ap.add_argument("--scale", type=int, default=6, help="样张放大倍数（默认 6，最近邻）")
    ap.add_argument("--dark", action="store_true", help="样张用深色背景")
    ap.add_argument("--gallery", type=Path, metavar="DIR", help="每类素材输出一张总览图到目录 DIR")
    ap.add_argument("--diag", type=int, metavar="N", help="随机 N 个组合，统计质量过滤的拒绝率与‘罪魁’素材")
    ap.add_argument("--check", action="store_true", help="只检查仓库里的 parts.json 是否与生成结果逐字节相同")
    ap.add_argument("--no-write", action="store_true", help="不写 parts.json（只在内存里生成并继续后面的操作）")
    args = ap.parse_args(argv)

    if args.check:
        fresh = parts_text(build_parts()).encode("utf-8")
        have = PARTS_PATH.read_bytes().replace(b"\r\n", b"\n") if PARTS_PATH.exists() else b""
        ok = fresh == have
        print(f"{PARTS_PATH}: {'与生成结果一致' if ok else '与生成结果不一致，请重新运行 tools/avatar_lab.py'}")
        return 0 if ok else 1

    parts = build_parts() if args.no_write else write_parts()
    counts = part_counts(parts)
    print("素材数量：" + "、".join(f"{k} {v}" for k, v in counts.items()))
    if not args.no_write:
        print(f"已写入 {PARTS_PATH}（{PARTS_PATH.stat().st_size / 1024:.0f} KB）")

    if args.gallery:
        for p in write_gallery(parts, args.gallery, scale=min(args.scale, 5), dark=args.dark):
            print("总览图：", p)
    if args.sheet:
        if not args.out:
            ap.error("--sheet 需要同时给出 --out")
        descs = sample_descs(parts, args.sheet, args.seed, roster=False if args.independent else None)
        cols = args.cols or (6 if args.sheet <= 12 else 12)
        write_png(render_sheet(parts, descs, scale=args.scale, cols=cols, dark=args.dark), args.out)
        print(f"样张：{args.out}（{len(descs)} 个头像）")
    if args.diag:
        diagnose(parts, args.diag, args.seed)
    return 0


def diagnose(parts: dict[str, Any], n: int, seed: int) -> None:
    """随机 n 个（不过滤的）组合，统计每条规则的拒绝率，并列出“罪魁”素材。"""
    ids = av.part_ids(parts)
    rng = Rng(f"diag/{seed}")
    reasons: Counter[str] = Counter()
    blame: dict[str, Counter[str]] = {f: Counter() for f in av.FIELDS}
    seen: dict[str, Counter[str]] = {f: Counter() for f in av.FIELDS}
    rejected = 0
    for _ in range(n):
        desc: dict[str, Any] = {"v": 1}
        for f in av.FIELDS:
            opts = ids[f]
            desc[f] = opts[rng.randrange(len(opts))]
        bad = check_description(parts, desc)
        for f in av.FIELDS:
            seen[f][desc[f]] += 1
        if bad:
            rejected += 1
            for r in bad:
                reasons[r] += 1
            for f in ("face", "hair", "eyes", "mouth", "accessory"):
                blame[f][desc[f]] += 1
    print(f"随机 {n} 个组合：被质量过滤拒绝 {rejected}（{rejected * 100.0 / n:.1f}%）")
    for r, c in reasons.most_common():
        print(f"  {r:18s} {c:6d}  {c * 100.0 / n:5.1f}%  {av.REASONS.get(r, '')}")
    for f in ("face", "hair", "eyes", "mouth", "accessory"):
        rates = sorted(((blame[f][i] / max(1, seen[f][i]), i) for i in ids[f]), reverse=True)
        top = "，".join(f"{i}:{r * 100:.0f}%" for r, i in rates[:8])
        print(f"  最易被拒的{f}：{top}")


if __name__ == "__main__":
    raise SystemExit(main())
