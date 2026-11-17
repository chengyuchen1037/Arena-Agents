"""像素头像（v0.12.1，E15）：素材库读取、描述校验、渲染、质量过滤与随机挑选。

纯函数、纯标准库，不依赖 FastAPI。素材库 ``static/avatar/parts.json`` 由 ``tools/avatar_lab.py`` 用算法生成
（确定性：固定种子，重跑得到同一份文件）；契约见 ``docs/v0.12-contracts.md`` §10.4。

**头像描述**（保存在名册里）::

    {"v": 1, "face": "f03", "skin": "s05", "hair": "h12", "hair_color": "c07",
     "eyes": "e04", "mouth": "m02", "accessory": "a09", "accessory_color": "k03"}

**渲染规则**（与前端 ``static/js/avatar.js`` 逐像素一致；同一描述在任何地方得到同一幅 20×20 像素）：

* 绘制顺序 ``hair.back → accessory.back → face → eyes → mouth → hair.front → accessory.front``，后画的逐像素覆盖先画的；
* 每层是 20 行 × 20 列的字符画：``.`` 透明，``o`` 描边，``x`` 腮红，``w`` 眼白，``p`` 瞳孔，``m`` 嘴，``t`` 牙齿
  （这六种颜色取自 ``palettes.fixed``，与所在的层无关）；
* ``b/s/l`` = 基色 / 暗部 / 亮部，取所在层的调色板：``hair`` 层用 ``hair_color``，``accessory`` 层用
  ``accessory_color``，**``face`` / ``eyes`` / ``mouth`` 三层都用 ``skin``**（前端就是这么实现的，眼睑、嘴角阴影因此可以
  直接用肤色）；
* 缺调色板或未知字符的像素被跳过；``accessory == "none"`` 等同 ``a00``（无配饰）。

**质量过滤**（``check_description`` / ``random_description``）拒绝不合格的组合：发色与肤色太接近、配饰或头发遮住眼睛 /
嘴、眼睛嘴巴画到脸外、脸部可见像素太少、轮廓不封闭（有空洞或漏口）、整体过暗 / 过亮、头发从帽子上方穿出来、配饰几乎看
不见、配饰与底色太接近。判断全部基于渲染出的像素，不依赖额外的元数据。

**名册**：``make_description`` 为一个座位挑一个与已有头像都明显不同的新描述（任意两个至少 3 项不同，肤色与发色不会同时
相同）；``make_roster`` 一次挑 n 个。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "SIZE",
    "DESC_VERSION",
    "FIELDS",
    "NO_ACCESSORY",
    "NO_HAIR",
    "PARTS_PATH",
    "load_parts",
    "validate_parts",
    "validate_description",
    "part_ids",
    "compose",
    "compose_chars",
    "render_pixels",
    "pixels_hex",
    "pixels_sha256",
    "pixels_fnv",
    "description_hash",
    "analyze_description",
    "check_description",
    "random_description",
    "pick_roster",
    "make_description",
    "make_roster",
    "description_distance",
    "REASONS",
]

SIZE = 20
DESC_VERSION = 1
#: 描述里的素材字段（也是“项”的定义：两个描述“有几项不同”按这 8 个字段数）
FIELDS = ("face", "skin", "hair", "hair_color", "eyes", "mouth", "accessory", "accessory_color")
NO_ACCESSORY = "a00"
NO_HAIR = "h00"
PARTS_PATH = Path(__file__).resolve().parent.parent / "static" / "avatar" / "parts.json"

#: 素材类别 -> (描述字段, 所在位置)
_SPRITE_CATEGORIES = ("face", "hair", "eyes", "mouth", "accessory")
_PALETTE_FIELDS = {"skin": "skin", "hair_color": "hair_color", "accessory_color": "accessory_color"}
_FIXED_KEYS = ("outline", "blush", "eye_white", "pupil", "mouth", "tooth")
_FIXED_CHARS = {"o": "outline", "x": "blush", "w": "eye_white", "p": "pupil", "m": "mouth", "t": "tooth"}
_FIELD_LABEL = {
    "face": "脸型",
    "skin": "肤色",
    "hair": "发型",
    "hair_color": "发色",
    "eyes": "眼睛",
    "mouth": "嘴",
    "accessory": "配饰",
    "accessory_color": "配饰颜色",
}
#: 绘制顺序：(层名, 来源类别, 取 back / front, 调色板字段)
_LAYERS = (
    ("hair.back", "hair", "back", "hair_color"),
    ("accessory.back", "accessory", "back", "accessory_color"),
    ("face", "face", "front", "skin"),
    ("eyes", "eyes", "front", "skin"),
    ("mouth", "mouth", "front", "skin"),
    ("hair.front", "hair", "front", "hair_color"),
    ("accessory.front", "accessory", "front", "accessory_color"),
)
_L_HAIR_BACK, _L_ACC_BACK, _L_FACE, _L_EYES, _L_MOUTH, _L_HAIR_FRONT, _L_ACC_FRONT = range(7)

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
_ID_RE = re.compile(r"^[a-z][0-9]{2,3}$")


# ----------------------------------------------------------------------------------------------------------------
# 素材库
# ----------------------------------------------------------------------------------------------------------------

_PARTS_CACHE: dict[str, Any] = {}


def _hex_rgb(value: Any) -> tuple[int, int, int] | None:
    """与前端 ``hexToRgb`` 同一规则：``#rrggbb`` / ``#rgb``（可省略 ``#``，不分大小写）。"""
    m = _HEX_RE.match(str(value or "").strip())
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c + c for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _is_grid(grid: Any) -> bool:
    return (
        isinstance(grid, (list, tuple))
        and len(grid) == SIZE
        and all(isinstance(row, str) and len(row) == SIZE for row in grid)
    )


def validate_parts(parts: Any) -> list[str]:
    """素材库的结构检查；返回问题列表（空 = 合格）。只检查结构与字符，不评价画得好不好看。"""
    errors: list[str] = []
    if not isinstance(parts, dict):
        return ["素材库必须是对象"]
    if parts.get("version") != 1:
        errors.append(f"version 必须是 1，收到 {parts.get('version')!r}")
    if parts.get("size") != SIZE:
        errors.append(f"size 必须是 {SIZE}，收到 {parts.get('size')!r}")
    pal = parts.get("palettes")
    if not isinstance(pal, dict):
        errors.append("缺少 palettes")
        pal = {}
    for field, key in _PALETTE_FIELDS.items():
        group = pal.get(key)
        if not isinstance(group, dict) or not group:
            errors.append(f"palettes.{key} 缺失或为空")
            continue
        for pid, entry in group.items():
            if not _ID_RE.match(pid):
                errors.append(f"palettes.{key}.{pid}: id 格式不对")
            if not isinstance(entry, dict) or any(_hex_rgb(entry.get(k)) is None for k in ("b", "s", "l")):
                errors.append(f"palettes.{key}.{pid}: 需要 b/s/l 三个 #rrggbb 颜色")
    fixed = pal.get("fixed")
    if not isinstance(fixed, dict):
        errors.append("palettes.fixed 缺失")
    else:
        for key in _FIXED_KEYS:
            if _hex_rgb(fixed.get(key)) is None:
                errors.append(f"palettes.fixed.{key} 不是合法的颜色")
    sprites = parts.get("parts")
    if not isinstance(sprites, dict):
        errors.append("缺少 parts")
        return errors
    for cat in _SPRITE_CATEGORIES:
        group = sprites.get(cat)
        if not isinstance(group, dict) or not group:
            errors.append(f"parts.{cat} 缺失或为空")
            continue
        for pid, entry in group.items():
            where = f"parts.{cat}.{pid}"
            if not _ID_RE.match(pid):
                errors.append(f"{where}: id 格式不对")
            if not isinstance(entry, dict):
                errors.append(f"{where}: 必须是对象")
                continue
            for side in ("back", "front"):
                grid = entry.get(side)
                if side == "back" and cat not in ("hair", "accessory"):
                    if grid is not None:
                        errors.append(f"{where}.back: {cat} 没有 back 层")
                    continue
                if grid is None:
                    if cat in ("face", "eyes", "mouth") and side == "front":
                        errors.append(f"{where}.front 不能为空")
                    continue
                if not _is_grid(grid):
                    errors.append(f"{where}.{side}: 必须是 {SIZE} 行、每行 {SIZE} 个字符")
                    continue
                bad = sorted({ch for row in grid for ch in row if ch not in ".obslxwpmt"})
                if bad:
                    errors.append(f"{where}.{side}: 含有非法字符 {''.join(bad)!r}")
    if isinstance(sprites.get("accessory"), dict):
        a00 = sprites["accessory"].get(NO_ACCESSORY)
        if not (isinstance(a00, dict) and a00.get("back") is None and a00.get("front") is None):
            errors.append("accessory.a00 必须存在且 back / front 都是 null（无配饰）")
    return errors


def load_parts(path: str | Path | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """读取素材库（默认 ``static/avatar/parts.json``）。结果按文件修改时间缓存，**调用方不得修改返回的对象**。

    文件缺失抛 :class:`FileNotFoundError`，内容损坏抛 :class:`ValueError`，消息里写明原因与修复办法。
    """
    p = Path(path) if path is not None else PARTS_PATH
    try:
        st = p.stat()
    except OSError as exc:
        raise FileNotFoundError(
            f"像素头像素材库不存在或无法读取：{p}。请先运行 `python tools/avatar_lab.py` 生成 static/avatar/parts.json。"
        ) from exc
    key = str(p)
    cached = _PARTS_CACHE.get(key)
    if not refresh and cached is not None and cached["stamp"] == (st.st_mtime_ns, st.st_size):
        return cached["data"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"像素头像素材库不是合法的 JSON：{p}（{exc}）。请重新运行 tools/avatar_lab.py 生成。") from exc
    problems = validate_parts(data)
    if problems:
        shown = "；".join(problems[:5]) + (f"（共 {len(problems)} 个问题）" if len(problems) > 5 else "")
        raise ValueError(f"像素头像素材库结构不合格：{shown}。请重新运行 tools/avatar_lab.py 生成。")
    _PARTS_CACHE[key] = {"stamp": (st.st_mtime_ns, st.st_size), "data": data}
    return data


def part_ids(parts: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """每个描述字段可选的 id（排序后）；``accessory`` 含 ``a00``。"""
    parts = parts if parts is not None else load_parts()
    sprites, pal = parts["parts"], parts["palettes"]
    out = {}
    for field in FIELDS:
        group = pal[_PALETTE_FIELDS[field]] if field in _PALETTE_FIELDS else sprites[field]
        out[field] = sorted(group)
    return out


# ----------------------------------------------------------------------------------------------------------------
# 描述
# ----------------------------------------------------------------------------------------------------------------


def validate_description(desc: Any, parts: dict[str, Any] | None = None) -> dict[str, Any]:
    """检查并规范化头像描述；不合格抛 ``ValueError``（中文原因）。

    规则：必须是 dict；只能含 ``v`` 和 8 个素材字段（缺一不可）；``v`` 必须是整数 1；每个 id 都必须存在于素材库
    （``accessory`` 的 ``"none"`` 会规范成 ``"a00"``）。返回新的 dict，键序固定。
    """
    if not isinstance(desc, dict):
        raise ValueError(f"头像描述必须是对象（dict），收到 {type(desc).__name__}")
    extra = sorted(str(k) for k in desc if k != "v" and k not in FIELDS)
    if extra:
        raise ValueError(f"头像描述含有不允许的键：{'、'.join(extra)}（只能有 v 和 {'、'.join(FIELDS)}）")
    version = desc.get("v")
    if isinstance(version, bool) or version != DESC_VERSION:
        raise ValueError(f"头像描述的版本 v 必须是 {DESC_VERSION}，收到 {version!r}")
    missing = [k for k in FIELDS if k not in desc]
    if missing:
        raise ValueError(f"头像描述缺少键：{'、'.join(missing)}")
    parts = parts if parts is not None else load_parts()
    ids = part_ids(parts)
    out: dict[str, Any] = {"v": DESC_VERSION}
    for field in FIELDS:
        value = desc[field]
        if not isinstance(value, str):
            raise ValueError(f"头像描述的 {field}（{_FIELD_LABEL[field]}）必须是字符串，收到 {type(value).__name__}")
        if field == "accessory" and value == "none":
            value = NO_ACCESSORY
        if value not in ids[field]:
            raise ValueError(f"头像描述的 {field}（{_FIELD_LABEL[field]}）含有未知的 id：{value!r}")
        out[field] = value
    return out


# ----------------------------------------------------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------------------------------------------------


@lru_cache(maxsize=16384)
def _sparse(grid: tuple[str, ...]) -> tuple[tuple[int, int, str], ...]:
    """一层字符画里所有非透明像素 ``(x, y, 字符)``（按行优先）。"""
    return tuple((x, y, ch) for y, row in enumerate(grid) for x, ch in enumerate(row) if ch != ".")


def _palette(entry: Any) -> dict[str, tuple[int, int, int]] | None:
    """与前端 ``paletteRgb`` 一致：必须有合法的 ``b``；``s`` / ``l`` 缺失时用 ``b``。"""
    if not isinstance(entry, dict):
        return None
    b = _hex_rgb(entry.get("b"))
    if b is None:
        return None
    return {"b": b, "s": _hex_rgb(entry.get("s")) or b, "l": _hex_rgb(entry.get("l")) or b}


def _lookup(parts: dict[str, Any], desc: dict[str, Any]) -> tuple[list, dict]:
    """把描述解析成 7 层 ``[(grid 或 None, 调色板 或 None)]`` 与 fixed 颜色表。"""
    sprites, pal = parts["parts"], parts["palettes"]
    fixed = {k: _hex_rgb(pal["fixed"].get(k)) for k in _FIXED_KEYS}
    picked: dict[str, Any] = {}
    for cat in _SPRITE_CATEGORIES:
        sid = desc[cat]
        if cat == "accessory" and sid == "none":
            picked[cat] = None
            continue
        entry = sprites[cat].get(sid)
        if not isinstance(entry, dict):
            raise ValueError(f"素材库里没有 {_FIELD_LABEL[cat]} {sid!r}")
        picked[cat] = entry
    palettes = {}
    for field, key in _PALETTE_FIELDS.items():
        palettes[field] = _palette(pal[key].get(desc[field]))
        if palettes[field] is None and (field != "accessory_color" or picked["accessory"] is not None):
            raise ValueError(f"素材库里没有{_FIELD_LABEL[field]} {desc[field]!r}")
    layers = []
    for _name, cat, side, pal_field in _LAYERS:
        entry = picked[cat]
        grid = entry.get(side) if entry else None
        layers.append((tuple(grid) if _is_grid(grid) else None, palettes[pal_field]))
    return layers, fixed


def compose_chars(parts: dict[str, Any], desc: dict[str, Any]) -> tuple[list[str], list[int]]:
    """逐像素叠层后的“字符”与“来源层”：各 400 项（行优先）；透明为 ``"."`` / ``-1``。

    来源层编号：0 hair.back、1 accessory.back、2 face、3 eyes、4 mouth、5 hair.front、6 accessory.front。
    与 :func:`compose` 同一套规则（缺调色板的 b/s/l 像素被跳过）。
    """
    layers, _fixed = _lookup(parts, desc)
    chars = ["."] * (SIZE * SIZE)
    src = [-1] * (SIZE * SIZE)
    for index, (grid, palette) in enumerate(layers):
        if grid is None:
            continue
        for x, y, ch in _sparse(grid):
            if ch in "bsl" and palette is None:
                continue
            i = y * SIZE + x
            chars[i] = ch
            src[i] = index
    return chars, src


def compose(parts: dict[str, Any], desc: dict[str, Any]) -> list[list[tuple[int, int, int, int]]]:
    """把描述合成为 20×20 的 ``(r, g, b, a)`` 像素矩阵（透明为 ``(0, 0, 0, 0)``，不透明 a=255）。

    纯函数；与前端 ``ArenaAvatar.renderPixels`` 逐像素一致。``desc`` 需是合法描述（见 :func:`validate_description`）。
    """
    layers, fixed = _lookup(parts, desc)
    pixels: list[list[tuple[int, int, int, int]]] = [[(0, 0, 0, 0)] * SIZE for _ in range(SIZE)]
    for grid, palette in layers:
        if grid is None:
            continue
        for x, y, ch in _sparse(grid):
            if ch in "bsl":
                rgb = palette[ch] if palette else None
            else:
                rgb = fixed.get(_FIXED_CHARS.get(ch, ""))
            if rgb is None:
                continue
            pixels[y][x] = (rgb[0], rgb[1], rgb[2], 255)
    return pixels


def pixels_hex(pixels: Sequence[Sequence[tuple[int, int, int, int]]]) -> list[str]:
    """``compose`` 的结果 -> 行优先的 ``#rrggbbaa`` 列表（透明像素是 ``#00000000``）。"""
    return ["#%02x%02x%02x%02x" % px for row in pixels for px in row]


def pixels_sha256(pixels: Sequence[Sequence[tuple[int, int, int, int]]]) -> str:
    """黄金哈希：``sha256(",".join(行优先 400 个 #rrggbbaa))``，十六进制小写。前端对照时用同一规则。"""
    return hashlib.sha256(",".join(pixels_hex(pixels)).encode("ascii")).hexdigest()


def pixels_fnv(pixels: Sequence[Sequence[tuple[int, int, int, int]]]) -> str:
    """与前端 ``ArenaAvatar.pixelHash`` 同格式：``20x20:`` + RGBA 字节流的 FNV-1a 32 位（8 位小写十六进制）。"""
    h = 0x811C9DC5
    for row in pixels:
        for px in row:
            for byte in px:
                h = ((h ^ byte) * 0x01000193) & 0xFFFFFFFF
    return f"{len(pixels[0])}x{len(pixels)}:{h:08x}"


def render_pixels(desc: dict[str, Any], parts: dict[str, Any] | None = None) -> list[list[str]]:
    """20×20 的 ``"#rrggbbff"``（不透明）或 ``""``（透明）。描述先经 :func:`validate_description` 检查。"""
    parts = parts if parts is not None else load_parts()
    d = validate_description(desc, parts)
    return [["#%02x%02x%02x%02x" % px if px[3] else "" for px in row] for row in compose(parts, d)]


def description_hash(desc: dict[str, Any], parts: dict[str, Any] | None = None) -> str:
    """一个描述渲染结果的黄金哈希（见 :func:`pixels_sha256`）。"""
    parts = parts if parts is not None else load_parts()
    return pixels_sha256(compose(parts, validate_description(desc, parts)))


# ----------------------------------------------------------------------------------------------------------------
# 质量过滤
# ----------------------------------------------------------------------------------------------------------------

#: 过滤规则的阈值（经 tools/avatar_lab.py --diag 在样本上校准）
HAIR_SKIN_MIN_DIST = 58.0  # 发色与肤色的 RGB 距离下限
EYE_COVER_MAX = 0.30  # 眼睛像素被头发 / 配饰前景遮住的比例上限
MOUTH_COVER_MAX = 0.34
FACE_VISIBLE_MIN = 0.36  # 可见肤色像素 / 脸部填充像素
FACE_VISIBLE_MIN_PX = 34
LEAK_MAX = 0  # 轮廓漏口（不透明非描边像素的上下左右有透明像素）的上限
HOLE_MAX = 0  # 被不透明像素围住的透明空洞的上限
LUMA_MIN = 64.0  # 整体亮度（可见非描边像素的平均亮度）范围
LUMA_MAX = 222.0
HAIR_OVER_HAT_MAX = 1  # 头发穿出帽子上沿的像素数上限
ACCESSORY_VISIBLE_MIN = 0.5  # 配饰可见像素比例下限
ACCESSORY_BLEND_DIST = 46.0  # 配饰与被它盖住的底色（头发 / 肤色）的最小 RGB 距离
EYE_CONTRAST_MIN = 46.0  # 肤色亮度 - 瞳孔亮度
#: 眼睛所在的行带 / 嘴所在的行带（所有脸型共用同一套眼口位置）；用来判断配饰是不是“有意盖住”眼睛 / 嘴的那种
EYE_BAND = (9, 13)
MOUTH_BAND = (13, 17)

#: 过滤原因（稳定的英文代号）-> 中文说明
REASONS = {
    "hair_skin_close": "发色与肤色太接近",
    "eyes_covered": "眼睛被头发或配饰遮住",
    "mouth_covered": "嘴被头发或配饰遮住",
    "eyes_off_face": "眼睛画到了脸的外面",
    "mouth_off_face": "嘴画到了脸的外面",
    "face_hidden": "脸部可见的像素太少",
    "silhouette_leak": "轮廓没有闭合（有漏口）",
    "silhouette_hole": "轮廓里有空洞",
    "too_dark": "整体太暗",
    "too_bright": "整体太亮",
    "hair_over_hat": "头发从帽子上方穿了出来",
    "accessory_hidden": "配饰几乎被头发或脸挡住",
    "accessory_blends": "配饰与底色太接近",
    "eye_contrast": "眼睛与肤色对比太弱",
}


def _luma(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _dist(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def _acc_kind(front: tuple[tuple[int, int, str], ...]) -> dict[str, int]:
    """配饰前景的像素在三个行带里各有多少：头顶（<EYE_BAND 起点）、眼带、下半脸。"""
    top = sum(1 for _x, y, _c in front if y < EYE_BAND[0])
    eyes = sum(1 for _x, y, _c in front if EYE_BAND[0] <= y < EYE_BAND[1])
    low = sum(1 for _x, y, _c in front if y >= MOUTH_BAND[0])
    return {"top": top, "eyes": eyes, "low": low, "all": len(front)}


def analyze_description(parts: dict[str, Any], desc: dict[str, Any]) -> dict[str, Any]:
    """一个描述的质量指标（字典），:func:`check_description` 据此给出拒绝原因。"""
    layers, fixed = _lookup(parts, desc)
    chars, src = compose_chars(parts, desc)
    pal = parts["palettes"]
    skin = _palette(pal["skin"][desc["skin"]]) or {}
    hair = _palette(pal["hair_color"][desc["hair_color"]]) or {}
    acc_pal = _palette(pal["accessory_color"].get(desc["accessory_color"])) if desc["accessory"] not in ("none", NO_ACCESSORY) else None
    out: dict[str, Any] = {"dist_hair_skin": _dist(hair["b"], skin["b"]) if desc["hair"] != NO_HAIR else 999.0}

    grid_of = [layer[0] for layer in layers]
    sp = [(_sparse(g) if g is not None else ()) for g in grid_of]
    eyes_px = [(x, y) for x, y, _c in sp[_L_EYES]]
    mouth_px = [(x, y) for x, y, _c in sp[_L_MOUTH]]
    face_fill = {(x, y) for x, y, c in sp[_L_FACE] if c in "bsl"}
    face_all = {(x, y) for x, y, _c in sp[_L_FACE]}

    kind = _acc_kind(sp[_L_ACC_FRONT]) if sp[_L_ACC_FRONT] else {"top": 0, "eyes": 0, "low": 0, "all": 0}
    eyewear = kind["eyes"] >= 6
    mouthwear = kind["low"] >= 6
    headwear = kind["top"] >= 10 and not eyewear

    def covered(points: list[tuple[int, int]], by_acc: bool) -> float:
        if not points:
            return 0.0
        layers_ = (_L_HAIR_FRONT, _L_ACC_FRONT) if by_acc else (_L_HAIR_FRONT,)
        n = sum(1 for x, y in points if src[y * SIZE + x] in layers_)
        return n / len(points)

    out["eyes_cover_hair"] = covered(eyes_px, False)
    out["eyes_cover_all"] = covered(eyes_px, True)
    out["mouth_cover_hair"] = covered(mouth_px, False)
    out["mouth_cover_all"] = covered(mouth_px, True)
    out["eyewear"], out["mouthwear"], out["headwear"] = eyewear, mouthwear, headwear
    out["eyes_off"] = sum(1 for p in eyes_px if p not in face_fill)
    out["mouth_off"] = sum(1 for p in mouth_px if p not in face_fill)

    visible_skin = sum(1 for i in range(SIZE * SIZE) if src[i] == _L_FACE and chars[i] in "bsl")
    out["face_visible"] = visible_skin
    out["face_visible_ratio"] = visible_skin / max(1, len(face_fill))

    # 轮廓：漏口（有色非描边像素的 4 邻有透明或出界）与空洞（被围住的透明区）
    leaks = 0
    for i in range(SIZE * SIZE):
        ch = chars[i]
        if ch in ".o":
            continue
        x, y = i % SIZE, i // SIZE
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < SIZE and 0 <= ny < SIZE) or chars[ny * SIZE + nx] == ".":
                leaks += 1
                break
    out["leaks"] = leaks
    seen = [False] * (SIZE * SIZE)
    stack = [(x, y) for x in range(SIZE) for y in (0, SIZE - 1)] + [(x, y) for y in range(SIZE) for x in (0, SIZE - 1)]
    while stack:
        x, y = stack.pop()
        i = y * SIZE + x
        if seen[i] or chars[i] != ".":
            continue
        seen[i] = True
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < SIZE and 0 <= ny < SIZE and not seen[ny * SIZE + nx]:
                stack.append((nx, ny))
    out["holes"] = sum(1 for i in range(SIZE * SIZE) if chars[i] == "." and not seen[i])

    # 亮度：可见的非描边像素的平均亮度
    total, count = 0.0, 0
    for i in range(SIZE * SIZE):
        ch = chars[i]
        if ch == ".":
            continue
        rgb = fixed.get(_FIXED_CHARS.get(ch, "")) if ch not in "bsl" else (_pal_of(i, src, skin, hair, acc_pal) or {}).get(ch)
        if ch == "o" or rgb is None:
            continue
        total += _luma(rgb)
        count += 1
    out["luma"] = total / count if count else 0.0

    # 头发穿出帽子上沿：同一列里，配饰前景的最高像素之上还有头发前景像素
    over = 0
    if headwear and sp[_L_HAIR_FRONT]:
        top_of: dict[int, int] = {}
        for x, y, _c in sp[_L_ACC_FRONT]:
            if x not in top_of or y < top_of[x]:
                top_of[x] = y
        for x, y, _c in sp[_L_HAIR_FRONT]:
            if x in top_of and y < top_of[x] and src[y * SIZE + x] == _L_HAIR_FRONT:
                over += 1
    out["hair_over_hat"] = over

    # 配饰可见比例
    acc_total = len(sp[_L_ACC_BACK]) + len(sp[_L_ACC_FRONT])
    if acc_total:
        acc_vis = sum(1 for i in range(SIZE * SIZE) if src[i] in (_L_ACC_BACK, _L_ACC_FRONT))
        out["accessory_visible"] = acc_vis / acc_total
    else:
        out["accessory_visible"] = 1.0

    # 配饰与底色太接近：配饰 b 像素压在头发 / 肤色上时，颜色要有足够差别
    blend = False
    if acc_pal is not None:
        on_hair = sum(1 for x, y, c in sp[_L_ACC_FRONT] if c == "b" and (x, y) in _hair_front_set(sp))
        on_skin = sum(1 for x, y, c in sp[_L_ACC_FRONT] if c == "b" and (x, y) in face_all and (x, y) not in _hair_front_set(sp))
        if on_hair >= 8 and desc["hair"] != NO_HAIR and _dist(acc_pal["b"], hair["b"]) < ACCESSORY_BLEND_DIST:
            blend = True
        if on_skin >= 8 and _dist(acc_pal["b"], skin["b"]) < ACCESSORY_BLEND_DIST:
            blend = True
    out["accessory_blends"] = blend

    pupil = fixed.get("pupil") or (0, 0, 0)
    out["eye_contrast"] = _luma(skin["b"]) - _luma(pupil)
    return out


def _hair_front_set(sp: list) -> set[tuple[int, int]]:
    return {(x, y) for x, y, _c in sp[_L_HAIR_FRONT]}


def _pal_of(index: int, src: list[int], skin: dict, hair: dict, acc: dict | None) -> dict | None:
    layer = src[index]
    if layer in (_L_FACE, _L_EYES, _L_MOUTH):
        return skin
    if layer in (_L_HAIR_BACK, _L_HAIR_FRONT):
        return hair
    return acc


def check_description(parts: dict[str, Any], desc: dict[str, Any]) -> list[str]:
    """质量过滤：返回拒绝原因的代号列表（见 ``REASONS``），空列表表示合格。"""
    m = analyze_description(parts, desc)
    bad: list[str] = []
    if m["dist_hair_skin"] < HAIR_SKIN_MIN_DIST:
        bad.append("hair_skin_close")
    if m["eyes_cover_hair"] > EYE_COVER_MAX or (not m["eyewear"] and m["eyes_cover_all"] > EYE_COVER_MAX):
        bad.append("eyes_covered")
    if m["mouth_cover_hair"] > MOUTH_COVER_MAX or (not m["mouthwear"] and m["mouth_cover_all"] > MOUTH_COVER_MAX):
        bad.append("mouth_covered")
    if m["eyes_off"]:
        bad.append("eyes_off_face")
    if m["mouth_off"]:
        bad.append("mouth_off_face")
    if m["face_visible"] < FACE_VISIBLE_MIN_PX or m["face_visible_ratio"] < FACE_VISIBLE_MIN:
        bad.append("face_hidden")
    if m["leaks"] > LEAK_MAX:
        bad.append("silhouette_leak")
    if m["holes"] > HOLE_MAX:
        bad.append("silhouette_hole")
    if m["luma"] < LUMA_MIN:
        bad.append("too_dark")
    if m["luma"] > LUMA_MAX:
        bad.append("too_bright")
    if m["hair_over_hat"] > HAIR_OVER_HAT_MAX:
        bad.append("hair_over_hat")
    if m["accessory_visible"] < ACCESSORY_VISIBLE_MIN:
        bad.append("accessory_hidden")
    if m["accessory_blends"]:
        bad.append("accessory_blends")
    if m["eye_contrast"] < EYE_CONTRAST_MIN:
        bad.append("eye_contrast")
    return bad


# ----------------------------------------------------------------------------------------------------------------
# 随机挑选
# ----------------------------------------------------------------------------------------------------------------

#: 随机挑配饰时“无配饰”的概率（其余配饰均匀）
NO_ACCESSORY_PROB = 0.22
MIN_DIFFERENT_FIELDS = 3  # 名册里任意两个头像至少有几项不同


def _effective(desc: dict[str, Any]) -> dict[str, Any]:
    """视觉上有意义的字段：没有头发时发色无意义，没有配饰时配饰颜色无意义。"""
    out = {f: desc.get(f) for f in FIELDS}
    if out["hair"] == NO_HAIR:
        out["hair_color"] = None
    if out["accessory"] in (NO_ACCESSORY, "none"):
        out["accessory"] = NO_ACCESSORY
        out["accessory_color"] = None
    return out


def description_distance(a: dict[str, Any], b: dict[str, Any]) -> int:
    """两个描述有几项（共 8 项）不同；没有头发 / 没有配饰时，对应的颜色项不计。"""
    ea, eb = _effective(a), _effective(b)
    return sum(1 for f in FIELDS if ea[f] != eb[f])


def _compatible(a: dict[str, Any], b: dict[str, Any], min_diff: int) -> bool:
    if description_distance(a, b) < min_diff:
        return False
    ea, eb = _effective(a), _effective(b)
    return not (ea["skin"] == eb["skin"] and ea["hair_color"] == eb["hair_color"])


def _color(parts: dict[str, Any], palette: str, pid: str) -> tuple[int, int, int]:
    return _hex_rgb(parts["palettes"][palette][pid]["b"]) or (0, 0, 0)


def _spread(parts: dict[str, Any], cand: dict[str, Any], others: Sequence[dict[str, Any]]) -> float:
    """候选与已有头像“有多不同”的分数（越大越好）：最小的项数差 + 一点颜色差。"""
    best = 1e9
    for o in others:
        score = float(description_distance(cand, o))
        try:
            score += _dist(_color(parts, "hair_color", cand["hair_color"]), _color(parts, "hair_color", o["hair_color"])) / 400.0
            score += _dist(_color(parts, "skin", cand["skin"]), _color(parts, "skin", o["skin"])) / 400.0
        except (KeyError, TypeError):
            pass
        best = min(best, score)
    return best


def random_description(parts: dict[str, Any], rng: Any, *, max_tries: int = 600) -> dict[str, Any]:
    """随机挑一项各类素材，经质量过滤后返回；``rng`` 是 ``random.Random`` 或同样有 ``random()`` / ``randrange()`` 的对象。"""
    ids = part_ids(parts)
    for _ in range(max_tries):
        desc: dict[str, Any] = {"v": DESC_VERSION}
        for field in FIELDS:
            options = ids[field]
            if field == "accessory" and NO_ACCESSORY in options:
                rest = [o for o in options if o != NO_ACCESSORY]
                desc[field] = NO_ACCESSORY if (rng.random() < NO_ACCESSORY_PROB or not rest) else rest[rng.randrange(len(rest))]
            else:
                desc[field] = options[rng.randrange(len(options))]
        if not check_description(parts, desc):
            return desc
    raise RuntimeError(f"随机 {max_tries} 次都没有挑出通过质量过滤的头像，请检查素材库（tools/avatar_lab.py --diag）")


def pick_roster(
    parts: dict[str, Any],
    n: int,
    rng: Any,
    taken: Iterable[dict[str, Any]] = (),
    *,
    candidates: int = 6,
    max_tries: int = 400,
) -> list[dict[str, Any]]:
    """一次挑 ``n`` 个互相区分明显的描述：与 ``taken`` 及彼此之间，任意两个至少 3 项不同，且肤色和发色不会同时相同。

    每个位置先攒 ``candidates`` 个合格候选，取“离已有头像最远”的那个；素材库太小而无法满足时逐级放宽（3→2→1 项不同），
    保证总能返回 ``n`` 个通过质量过滤的描述。
    """
    pool = [d for d in taken if isinstance(d, dict)]
    chosen: list[dict[str, Any]] = []
    for _ in range(n):
        picked = None
        for min_diff in (MIN_DIFFERENT_FIELDS, 2, 1, 0):
            best, best_score, found = None, -1.0, 0
            for _try in range(max_tries):
                cand = random_description(parts, rng)
                others = pool + chosen
                if not all(_compatible(cand, o, min_diff) for o in others):
                    continue
                found += 1
                score = _spread(parts, cand, others) if others else 0.0
                if score > best_score:
                    best, best_score = cand, score
                if found >= candidates:
                    break
            if best is not None:
                picked = best
                break
        if picked is None:  # 理论上不会发生：min_diff=0 时任何候选都兼容
            picked = random_description(parts, rng)
        chosen.append(picked)
    return chosen


def _rng_for(seed: Any, salt: Any) -> random.Random:
    """``random.Random``；给了 seed 就可复现（字符串种子，跨平台、跨 Python 版本稳定）。"""
    if seed is None:
        return random.Random()
    return random.Random(f"arena-avatar:{seed}:{salt}")


def make_description(
    seat: int,
    taken: Sequence[dict[str, Any]],
    *,
    seed: int | None = None,
    parts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为座位 ``seat`` 挑一个与 ``taken`` 里每个头像都明显不同的新描述（随机；``seed`` 给定则可复现）。"""
    parts = parts if parts is not None else load_parts()
    return pick_roster(parts, 1, _rng_for(seed, seat), taken or ())[0]


def make_roster(n: int, *, seed: int | None = None, parts: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """一次挑 ``n`` 个互相区分明显的头像描述（``seed`` 给定则可复现）。"""
    parts = parts if parts is not None else load_parts()
    return pick_roster(parts, int(n), _rng_for(seed, "roster"))
