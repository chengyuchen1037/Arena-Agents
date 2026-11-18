"""v0.12.1 像素头像素材库（E15）：`tools/avatar_lab.py`（生成器）+ `static/avatar/parts.json`（生成物）+ `app/avatars.py`（后端）。

全部离线。覆盖：素材库结构与数量下限、字符画合法性、调色板、``a00`` 无配饰、渲染规则（绘制顺序 / 每层调色板）、
同一描述逐像素相同（同进程、跨进程、与前端 ``static/js/avatar.js`` 对拍）、黄金哈希、质量过滤、``pick_roster`` /
``make_description`` / ``make_roster``、``validate_description`` 的中文错误、生成的确定性（与仓库里的 parts.json
逐字节相同）、命令行样张。

黄金哈希（``GOLDEN``）锁定的是“这版素材库渲染出来的像素”。**有意改素材时**：运行 ``python tools/avatar_lab.py``
重新生成 parts.json，再用 ``python tests/test_avatar_lab.py`` 打印新的哈希，替换 ``GOLDEN`` 里的值，并同步给前端测试。
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import avatars as av  # noqa: E402

PARTS_FILE = ROOT / "static" / "avatar" / "parts.json"
LAB_FILE = ROOT / "tools" / "avatar_lab.py"
CJK = re.compile(r"[\u4e00-\u9fff]")
HEX = re.compile(r"^#[0-9a-f]{6}$")
LEGAL_CHARS = set(".obslxwpmt")
FIELDS = av.FIELDS

# 素材数量下限（任务书 §2；括号里是 docs/v0.12.1-plan.md §4.2 的目标）
MINIMUMS = {
    "face": 8,
    "skin": 12,
    "hair": 30,
    "hair_color": 14,
    "eyes": 14,
    "mouth": 10,
    "accessory": 24,  # 含 a00 = 无配饰
    "accessory_color": 10,
}

# 黄金像素哈希：对固定的 3 个描述，sha256(",".join(行优先 400 个 "#rrggbbaa"))；fnv 与前端 ArenaAvatar.pixelHash 同格式。
# 描述 1：中长发 + 帽子；描述 2：光头 + 无配饰（顶部全透明）；描述 3：带 back 层的长发 + 带 back 层的翅膀 + 腮红 / 牙齿。
GOLDEN = [
    (
        {"v": 1, "face": "f01", "skin": "s03", "hair": "h02", "hair_color": "c03", "eyes": "e01", "mouth": "m10", "accessory": "a09", "accessory_color": "k05"},
        "5c68f4e74b44552de2117e9ae8ef4c223e207ad71c7795e85ea48214b7601926",
        "20x20:1f05fde2",
    ),
    (
        {"v": 1, "face": "f04", "skin": "s09", "hair": "h00", "hair_color": "c00", "eyes": "e22", "mouth": "m09", "accessory": "a00", "accessory_color": "k05"},
        "3489cbb65dac11358e46fcdb7d1c50fb98a85599435ae83535e169ca78967d83",
        "20x20:6ea66521",
    ),
    (
        {"v": 1, "face": "f12", "skin": "s02", "hair": "h19", "hair_color": "c13", "eyes": "e03", "mouth": "m10", "accessory": "a37", "accessory_color": "k05"},
        "0dd05a0ac6d2f501483f2b1372b89d286d1b44339d5b3b37db839e3f44b01b28",
        "20x20:65c24234",
    ),
]


# ------------------------------------------------------------------------------------------------ fixtures / helpers
@pytest.fixture(scope="module")
def parts() -> dict:
    return json.loads(PARTS_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def lab():
    spec = importlib.util.spec_from_file_location("avatar_lab_under_test", LAB_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _node() -> str | None:
    env = os.environ.get("NODE_BIN")
    if env and Path(env).exists():
        return env
    found = shutil.which("node")
    if found:
        return found
    windows = Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"
    return str(windows) if windows.exists() else None


def _mask(entry: dict) -> set[tuple[int, int]]:
    out = set()
    for side in ("back", "front"):
        grid = entry.get(side)
        if grid:
            out |= {(x, y) for y, row in enumerate(grid) for x, ch in enumerate(row) if ch != "."}
    return out


def _distinct_fields(a: dict, b: dict) -> int:
    return sum(1 for f in FIELDS if a[f] != b[f])


def _flat(pixels) -> list[tuple[int, int, int, int]]:
    return [px for row in pixels for px in row]


def _blank_synthetic() -> dict:
    """最小的合成素材库：用来精确验证渲染规则（每层的调色板、绘制顺序、透明）。只用第 0 行的前 12 个像素。"""

    def row0(**cells: str) -> list[str]:  # 键 "c3" = 第 0 行第 3 列
        row = ["."] * 20
        for key, ch in cells.items():
            row[int(key[1:])] = ch
        return ["".join(row)] + ["." * 20] * 19

    pal = lambda b, s, l: {"b": b, "s": s, "l": l}  # noqa: E731
    return {
        "version": 1,
        "size": 20,
        "palettes": {
            "skin": {"s00": pal("#101010", "#111111", "#121212")},
            "hair_color": {"c00": pal("#202020", "#212121", "#222222")},
            "accessory_color": {"k00": pal("#303030", "#313131", "#323232")},
            "fixed": {"outline": "#aa0001", "blush": "#aa0002", "eye_white": "#aa0003", "pupil": "#aa0004", "mouth": "#aa0005", "tooth": "#aa0006"},
        },
        "parts": {
            "face": {"f00": {"front": row0(c0="b", c1="b", c2="b", c3="b", c4="b", c5="b", c6="b", c7="b", c8="b")}},
            "hair": {"h00": {"back": row0(c0="b", c1="b", c2="b", c3="b", c4="b", c5="b"), "front": row0(c0="b", c1="b", c2="b", c3="b", c5="s")}},
            "eyes": {"e00": {"front": row0(c0="p", c1="p", c2="p", c3="p", c4="w", c7="s")}},
            "mouth": {"m00": {"front": row0(c0="m", c1="m", c2="m", c3="t", c4="x", c8="l")}},
            "accessory": {
                "a00": {"back": None, "front": None},
                "a01": {"back": row0(c0="b", c1="b", c2="b", c9="b"), "front": row0(c0="o", c1="l", c2="s")},
            },
        },
    }


# ------------------------------------------------------------------------------------------------ 素材库结构
def test_parts_file_passes_structure_validation_and_minimum_counts(parts):
    assert av.validate_parts(parts) == []
    assert parts["version"] == 1 and parts["size"] == 20
    groups = {
        "face": parts["parts"]["face"],
        "skin": parts["palettes"]["skin"],
        "hair": parts["parts"]["hair"],
        "hair_color": parts["palettes"]["hair_color"],
        "eyes": parts["parts"]["eyes"],
        "mouth": parts["parts"]["mouth"],
        "accessory": parts["parts"]["accessory"],
        "accessory_color": parts["palettes"]["accessory_color"],
    }
    prefix = {"face": "f", "skin": "s", "hair": "h", "hair_color": "c", "eyes": "e", "mouth": "m", "accessory": "a", "accessory_color": "k"}
    for name, group in groups.items():
        assert len(group) >= MINIMUMS[name], f"{name}: {len(group)} < {MINIMUMS[name]}"
        # id 固定宽度、从 00 连续编号
        assert sorted(group) == [f"{prefix[name]}{i:02d}" for i in range(len(group))], name
    assert PARTS_FILE.stat().st_size < 1_500_000


def test_every_sprite_is_a_20x20_grid_of_legal_characters(parts):
    for category, group in parts["parts"].items():
        assert category in ("face", "hair", "eyes", "mouth", "accessory")
        for pid, entry in group.items():
            assert set(entry) <= {"back", "front"}, (category, pid)
            if category in ("face", "eyes", "mouth"):
                assert entry.get("back") in (None,) and isinstance(entry["front"], list), (category, pid)
            for side in ("back", "front"):
                grid = entry.get(side)
                if grid is None:
                    continue
                assert isinstance(grid, list) and len(grid) == 20, (category, pid, side)
                for row in grid:
                    assert isinstance(row, str) and len(row) == 20, (category, pid, side)
                    assert set(row) <= LEGAL_CHARS, (category, pid, side, set(row) - LEGAL_CHARS)
                assert any(ch != "." for row in grid for ch in row), f"{category}.{pid}.{side} 是空图（应该用 null）"


def test_palettes_are_valid_distinct_and_shaded(parts):
    luma = lambda h: 0.299 * int(h[1:3], 16) + 0.587 * int(h[3:5], 16) + 0.114 * int(h[5:7], 16)  # noqa: E731
    for name in ("skin", "hair_color", "accessory_color"):
        bases = set()
        for pid, entry in parts["palettes"][name].items():
            assert set(entry) == {"b", "s", "l"}, (name, pid)
            assert all(HEX.match(entry[k]) for k in "bsl"), (name, pid, entry)
            assert luma(entry["s"]) <= luma(entry["b"]) <= luma(entry["l"]) + 1e-9, (name, pid, entry)
            bases.add(entry["b"])
        assert len(bases) == len(parts["palettes"][name]), f"{name}: 基色有重复"
    fixed = parts["palettes"]["fixed"]
    assert set(fixed) == {"outline", "blush", "eye_white", "pupil", "mouth", "tooth"}
    assert all(HEX.match(v) for v in fixed.values())
    # 肤色里既有天然肤色、也要有奇幻色（灰 / 绿 / 蓝 / 紫 / 红……）
    weights = [av._skin_weight(av._hex_rgb(e["b"])) for e in parts["palettes"]["skin"].values()]
    assert sum(1 for w in weights if w == 1.0) >= 8 and sum(1 for w in weights if w < 1.0) >= 4


def test_a00_is_the_empty_accessory_and_hair_h00_is_bald(parts):
    assert parts["parts"]["accessory"]["a00"] == {"back": None, "front": None}
    assert parts["parts"]["hair"]["h00"] == {"back": None, "front": None}
    # 其余发型 / 配饰都至少有一个非空层；back 层和 front 层的素材都存在
    assert all(e["back"] or e["front"] for k, e in parts["parts"]["hair"].items() if k != "h00")
    assert all(e["back"] or e["front"] for k, e in parts["parts"]["accessory"].items() if k != "a00")
    assert sum(1 for e in parts["parts"]["hair"].values() if e["back"]) >= 8
    assert sum(1 for e in parts["parts"]["accessory"].values() if e["back"]) >= 1


def test_faces_are_closed_symmetric_outlined_and_leave_room_for_hair(parts):
    for fid, entry in parts["parts"]["face"].items():
        grid = entry["front"]
        mask = _mask(entry)
        assert all((19 - x, y) in mask for x, y in mask), f"{fid} 左右不对称"
        ys = [y for _x, y in mask]
        xs = [x for x, _y in mask]
        assert min(ys) >= 4, f"{fid}: 脸顶太高，头发 / 帽子盖不住（顶部描边应在第 4 行或更低）"
        assert max(ys) <= 18 and min(xs) >= 1 and max(xs) <= 18, f"{fid}: 脸贴到了画布边缘（不能画脖子）"
        outline = sum(row.count("o") for row in grid)
        assert outline >= 28, f"{fid}: 描边太少（{outline}）"
        # 轮廓闭合：任何有色像素的上下左右都不能是透明（只能是同色或描边）
        for y, row in enumerate(grid):
            for x, ch in enumerate(row):
                if ch in ".o":
                    continue
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    assert grid[y + dy][x + dx] != ".", f"{fid}: ({x},{y}) 的轮廓没有闭合"
        # 描边至少 1 像素厚，顶部 / 底部各有一整段
        assert "oooo" in grid[min(ys)] and "oooo" in grid[max(ys)], fid


def test_most_hair_is_left_right_symmetric_with_only_a_few_asymmetric_tips(parts):
    ratios = []
    for hid, entry in parts["parts"]["hair"].items():
        mask = _mask(entry)
        if mask:
            ratios.append(sum(1 for x, y in mask if (19 - x, y) not in mask) / len(mask))
    assert len(ratios) >= 30
    assert sum(1 for r in ratios if r <= 0.10) / len(ratios) >= 0.75  # 马尾 / 单侧辫子等少数发型故意不对称
    assert sum(1 for r in ratios if r > 0.0) >= 3  # 但发梢确实有少量不对称
    # 眼睛 / 嘴的模板：除眨眼和歪嘴外左右对称
    asym_eyes = [k for k, e in parts["parts"]["eyes"].items() if any((19 - x, y) not in _mask(e) for x, y in _mask(e))]
    assert 1 <= len(asym_eyes) <= 3


def test_eye_and_mouth_sprites_live_in_the_face_rows(parts):
    for eid, entry in parts["parts"]["eyes"].items():
        ys = [y for _x, y in _mask(entry)]
        assert 7 <= min(ys) and max(ys) <= 13, eid
    for mid, entry in parts["parts"]["mouth"].items():
        ys = [y for _x, y in _mask(entry)]
        assert 13 <= min(ys) and max(ys) <= 16, mid
    # 眼睛里有眼白 / 瞳孔 / 高光，嘴里有牙齿 / 舌头 / 腮红
    chars = {k: set("".join("".join(e["front"]) for e in parts["parts"][k].values())) for k in ("eyes", "mouth")}
    assert {"w", "p", "x"} <= chars["eyes"] and {"m", "t", "x"} <= chars["mouth"]


# ------------------------------------------------------------------------------------------------ 渲染规则
def test_render_order_and_per_layer_palettes_on_a_synthetic_library():
    lib = _blank_synthetic()
    desc = {"v": 1, "face": "f00", "skin": "s00", "hair": "h00", "hair_color": "c00", "eyes": "e00", "mouth": "m00", "accessory": "a01", "accessory_color": "k00"}
    assert av.validate_parts(lib) == []
    row = [av.compose(lib, desc)[0][x] for x in range(12)]
    rgb = lambda h: (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16), 255)  # noqa: E731
    skin, hair, acc, fixed = lib["palettes"]["skin"]["s00"], lib["palettes"]["hair_color"]["c00"], lib["palettes"]["accessory_color"]["k00"], lib["palettes"]["fixed"]
    # x0：七层全覆盖 → 最后画的 accessory.front 的描边
    assert row[0] == rgb(fixed["outline"])
    # x1：被 accessory.front 的 l 盖住 → 配饰色板的亮部（不是肤色 / 发色）
    assert row[1] == rgb(acc["l"])
    # x2：accessory.front 的 s → 配饰暗部
    assert row[2] == rgb(acc["s"])
    # x3：hair.front 的 b 压过 mouth(牙齿) / eyes / face / back 层 → 发色基色
    assert row[3] == rgb(hair["b"])
    # x4：hair.front 在这里透明('.')，露出下面的 mouth 腮红 'x'
    assert row[4] == rgb(fixed["blush"])
    # x5：hair.front 的 s → 发色暗部；
    assert row[5] == rgb(hair["s"])
    # x6：只有 face 的 b → 肤色基色
    assert row[6] == rgb(skin["b"])
    # x7：eyes 层的 's' 用【肤色】暗部（眼睑 / 眼袋）；x8：mouth 层的 'l' 用肤色亮部
    assert row[7] == rgb(skin["s"])
    assert row[8] == rgb(skin["l"])
    # x9：只有 accessory.back 的 b（配饰基色）；x10 / x11：没有任何层 → 透明
    assert row[9] == rgb(acc["b"])
    assert row[10] == (0, 0, 0, 0) and row[11] == (0, 0, 0, 0)
    # 没有配饰（a00 / "none"）时 accessory 层完全不画
    plain = av.compose(lib, {**desc, "accessory": "a00"})[0]
    assert plain[0] == rgb(hair["b"]) and plain[9] == (0, 0, 0, 0)
    assert av.compose(lib, {**desc, "accessory": "none"})[0] == plain
    # 眼睛 / 嘴：pupil / eye_white / mouth / tooth 的固定色
    only_eyes = copy.deepcopy(lib)
    only_eyes["parts"]["hair"]["h00"] = {"back": None, "front": None}
    out = av.compose(only_eyes, {**desc, "accessory": "a00"})[0]
    assert out[0] == rgb(fixed["mouth"]) and out[3] == rgb(fixed["tooth"]) and out[4] == rgb(fixed["blush"])
    only_eyes["parts"]["mouth"]["m00"] = {"front": ["." * 20] * 20}
    out = av.compose(only_eyes, {**desc, "accessory": "a00"})[0]
    assert out[0] == rgb(fixed["pupil"]) and out[4] == rgb(fixed["eye_white"])


def test_compose_returns_a_20x20_rgba_matrix_and_render_pixels_returns_hex_strings(parts):
    desc = GOLDEN[0][0]
    px = av.compose(parts, desc)
    assert len(px) == 20 and all(len(row) == 20 for row in px)
    assert all(len(p) == 4 and all(0 <= c <= 255 for c in p) for row in px for p in row)
    assert all((p[3] == 255) or p == (0, 0, 0, 0) for row in px for p in row)  # 不透明或完全透明
    hexes = av.render_pixels(desc, parts)
    assert len(hexes) == 20 and all(len(row) == 20 for row in hexes)
    assert all(h == "" or re.fullmatch(r"#[0-9a-f]{6}ff", h) for row in hexes for h in row)
    for y in range(20):
        for x in range(20):
            assert (hexes[y][x] == "") == (px[y][x][3] == 0)
    assert px[0][0] == (0, 0, 0, 0) and px[19][19] == (0, 0, 0, 0)  # 角落是透明的
    outline = av._hex_rgb(parts["palettes"]["fixed"]["outline"])
    assert sum(1 for p in _flat(px) if p[:3] == outline and p[3]) >= 30  # 有清晰的深色描边
    assert 150 <= sum(1 for p in _flat(px) if p[3]) <= 380


def test_same_description_renders_pixel_identically_in_process(parts):
    for desc, _sha, _fnv in GOLDEN:
        a, b = av.compose(parts, desc), av.compose(parts, desc)
        assert a == b
        assert av.render_pixels(desc, parts) == av.render_pixels(copy.deepcopy(desc), json.loads(json.dumps(parts)))
        assert av.description_hash(desc, parts) == av.description_hash(desc)  # 传不传 parts 都一样


def test_same_description_renders_pixel_identically_across_processes():
    code = "import json,sys; sys.path.insert(0, sys.argv[2]); from app import avatars as av; print(av.description_hash(json.loads(sys.argv[1])))"
    for desc, _sha, _fnv in GOLDEN:
        here = av.description_hash(desc)
        for hashseed in ("0", "4242"):
            env = dict(os.environ, PYTHONHASHSEED=hashseed, PYTHONUTF8="1")
            out = subprocess.run([sys.executable, "-c", code, json.dumps(desc), str(ROOT)], capture_output=True, text=True, env=env, timeout=60, cwd=str(ROOT))
            assert out.returncode == 0, out.stderr
            assert out.stdout.strip() == here


def test_golden_pixel_hashes(parts):
    for desc, sha, fnv in GOLDEN:
        px = av.compose(parts, av.validate_description(desc, parts))
        hexes = av.pixels_hex(px)
        assert len(hexes) == 400 and all(re.fullmatch(r"#[0-9a-f]{8}", h) for h in hexes)
        assert hashlib.sha256(",".join(hexes).encode("ascii")).hexdigest() == av.pixels_sha256(px)
        assert av.pixels_sha256(px) == sha, f"{desc} 的像素变了；若是有意改素材，请更新 GOLDEN（见本文件开头）"
        assert av.pixels_fnv(px) == fnv


def test_python_and_frontend_renderers_agree_pixel_for_pixel(parts, tmp_path):
    """用 node 直接跑前端 static/js/avatar.js 的 renderPixels，和 Python 的 compose 逐像素（sha256 + FNV）对拍。"""
    node = _node()
    if node is None:
        pytest.skip("node executable not found")
    rng = random.Random(20260617)
    ids = av.part_ids(parts)
    descs = [d for d, _s, _f in GOLDEN] + [av.random_description(parts, rng) for _ in range(40)]
    descs += [{"v": 1, **{f: rng.choice(ids[f]) for f in FIELDS}} for _ in range(40)]  # 不过滤的组合（含“丑”的）也要一致
    descs.append({**GOLDEN[0][0], "accessory": "none"})  # "none" 等同 a00
    script = (
        "const fs=require('fs'),crypto=require('crypto');"
        "const A=require(process.argv[1]);const parts=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));"
        "const descs=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));"
        "process.stdout.write(JSON.stringify(descs.map(d=>{const px=A.renderPixels(d,parts);if(!px)return null;const h=[];"
        "for(let i=0;i<400;i++){const o=i*4;h.push('#'+[0,1,2,3].map(k=>px.data[o+k].toString(16).padStart(2,'0')).join(''));}"
        "return {sha:crypto.createHash('sha256').update(h.join(',')).digest('hex'),fnv:A.pixelHash(px)};})));"
    )
    descs_file = tmp_path / "descs.json"
    descs_file.write_text(json.dumps(descs), encoding="utf-8")
    out = subprocess.run(
        [node, "-e", script, str(ROOT / "static" / "js" / "avatar.js"), str(PARTS_FILE), str(descs_file)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    js = json.loads(out.stdout)
    assert len(js) == len(descs)
    for desc, got in zip(descs, js):
        px = av.compose(parts, {**desc, "accessory": av.NO_ACCESSORY if desc["accessory"] == "none" else desc["accessory"]})
        assert got is not None, desc
        assert got["sha"] == av.pixels_sha256(px), desc
        assert got["fnv"] == av.pixels_fnv(px), desc


def test_different_descriptions_render_differently(parts):
    base = {"v": 1, "face": "f01", "skin": "s03", "hair": "h07", "hair_color": "c06", "eyes": "e01", "mouth": "m00", "accessory": "a05", "accessory_color": "k02"}
    ref = av.compose(parts, base)
    ids = av.part_ids(parts)
    for field in FIELDS:
        other = next(i for i in ids[field] if i != base[field] and i != "a00")
        changed = av.compose(parts, {**base, field: other})
        assert changed != ref, f"只改 {field} 应该改变像素"
    # 一整桌 12 人：像素两两不同
    roster = av.make_roster(12, seed=3, parts=parts)
    hashes = {av.pixels_sha256(av.compose(parts, d)) for d in roster}
    assert len(hashes) == 12
    # 许多随机描述的像素基本都不同
    rng = random.Random(1)
    many = {av.pixels_sha256(av.compose(parts, av.random_description(parts, rng))) for _ in range(150)}
    assert len(many) >= 148


# ------------------------------------------------------------------------------------------------ 质量过滤
def test_random_description_passes_quality_filter_on_500_seeds(parts):
    seen = {f: set() for f in FIELDS}
    no_accessory = 0
    for seed in range(500):
        desc = av.random_description(parts, random.Random(seed))
        assert av.check_description(parts, desc) == [], (seed, desc)
        assert av.validate_description(desc, parts) == desc
        px = av.render_pixels(desc, parts)
        assert len(px) == 20 and all(len(r) == 20 for r in px)
        assert any(h for r in px for h in r)
        for f in FIELDS:
            seen[f].add(desc[f])
        no_accessory += desc["accessory"] == av.NO_ACCESSORY
    ids = av.part_ids(parts)
    assert seen["face"] == set(ids["face"])  # 每种脸型都会被抽到
    assert len(seen["hair"]) >= 0.9 * len(ids["hair"]) and len(seen["accessory"]) >= 0.9 * len(ids["accessory"])
    assert len(seen["skin"]) >= 0.8 * len(ids["skin"]) and len(seen["hair_color"]) >= 0.9 * len(ids["hair_color"])
    assert 0.12 <= no_accessory / 500 <= 0.40


def test_random_description_is_reproducible_for_a_seed(parts):
    a = [av.random_description(parts, random.Random(s)) for s in range(30)]
    b = [av.random_description(parts, random.Random(s)) for s in range(30)]
    assert a == b


def test_every_part_can_appear_in_some_acceptable_avatar(parts):
    """没有“永远被质量过滤拒绝”的死素材：每个脸型 / 发型 / 眼睛 / 嘴 / 配饰都至少能组出一个合格的头像。"""
    ids = av.part_ids(parts)
    rng = random.Random(99)
    dead = []
    for field in ("face", "hair", "eyes", "mouth", "accessory"):
        for pid in ids[field]:
            for _ in range(80):
                desc = av.random_description(parts, rng)
                desc[field] = pid
                if av.check_description(parts, desc) == []:
                    break
            else:
                dead.append((field, pid))
    assert not dead, f"这些素材在 80 次随机组合里一次都没通过质量过滤：{dead}"


def test_quality_filter_rejection_rate_is_reasonable(lab, parts):
    summary = lab.diagnose(parts, 1500, 1, verbose=False)
    assert summary["rate"] < 0.35, summary  # 随机组合大多数都应该是合格的，否则“随机”会偏向少数素材
    assert summary["rate"] > 0.02  # 但过滤规则不是摆设


def _base_for_filter(parts) -> dict:
    return {"v": 1, "face": "f01", "skin": "s03", "hair": "h02", "hair_color": "c06", "eyes": "e01", "mouth": "m01", "accessory": "a00", "accessory_color": "k00"}


def test_quality_filter_accepts_the_golden_descriptions(parts):
    for desc, _s, _f in GOLDEN:
        assert av.check_description(parts, desc) == [], desc
    assert av.check_description(parts, _base_for_filter(parts)) == []


def test_quality_filter_rejects_hair_colour_too_close_to_skin(parts):
    lib = copy.deepcopy(parts)
    lib["palettes"]["hair_color"]["c06"] = copy.deepcopy(lib["palettes"]["skin"]["s03"])
    assert "hair_skin_close" in av.check_description(lib, _base_for_filter(lib))
    # 光头时发色无关紧要
    assert "hair_skin_close" not in av.check_description(lib, {**_base_for_filter(lib), "hair": "h00"})


def test_quality_filter_rejects_hair_covering_the_eyes_or_the_face(parts):
    base = _base_for_filter(parts)
    lib = copy.deepcopy(parts)
    blank = ["." * 20 for _ in range(20)]
    cover_eyes = list(blank)
    for y in range(8, 14):
        cover_eyes[y] = "." * 3 + "b" * 14 + "." * 3
    lib["parts"]["hair"]["h02"] = {"back": None, "front": cover_eyes}
    assert "eyes_covered" in av.check_description(lib, base)
    cover_face = list(blank)
    for y in range(4, 17):
        cover_face[y] = "." * 2 + "b" * 16 + "." * 2
    lib["parts"]["hair"]["h02"] = {"back": None, "front": cover_face}
    reasons = av.check_description(lib, base)
    assert "face_hidden" in reasons and "eyes_covered" in reasons and "mouth_covered" in reasons


def test_quality_filter_rejects_open_silhouettes_and_dark_or_bright_pictures(parts):
    base = _base_for_filter(parts)
    # 头顶出现空洞：脸型里挖掉一个内部像素（且头发是光头，露出来）
    lib = copy.deepcopy(parts)
    face = [list(r) for r in lib["parts"]["face"]["f01"]["front"]]
    face[8][9] = "."
    lib["parts"]["face"]["f01"]["front"] = ["".join(r) for r in face]
    assert "silhouette_hole" in av.check_description(lib, {**base, "hair": "h00"})
    # 轮廓漏口：把脸顶的一段描边抹掉
    lib = copy.deepcopy(parts)
    face = [list(r) for r in lib["parts"]["face"]["f01"]["front"]]
    for x in range(8, 12):
        face[4][x] = "."
    lib["parts"]["face"]["f01"]["front"] = ["".join(r) for r in face]
    assert "silhouette_leak" in av.check_description(lib, {**base, "hair": "h00"})
    # 整体太暗 / 太亮
    dark = copy.deepcopy(parts)
    for name in ("skin", "hair_color", "accessory_color"):
        for entry in dark["palettes"][name].values():
            entry.update(b="#0a0a0a", s="#050505", l="#101010")
    assert "too_dark" in av.check_description(dark, base)
    bright = copy.deepcopy(parts)
    for name in ("skin", "hair_color", "accessory_color"):
        for entry in bright["palettes"][name].values():
            entry.update(b="#fefefe", s="#f8f8f8", l="#ffffff")
    assert "too_bright" in av.check_description(bright, base)


def test_quality_filter_allows_eyewear_but_rejects_hair_poking_through_a_hat(lab, parts):
    names = [name for name, _fn, _opts in lab.accessory_recipes()]
    base = _base_for_filter(parts)
    # 眼镜类配饰（墨镜、护目镜、眼罩……）遮住眼睛是设计本意，不能因此被拒绝（否则墨镜永远抽不到）
    for name in ("sunglasses", "goggles", "visor", "eyepatch"):
        pid = f"a{names.index(name) + 1:02d}"
        reasons = av.check_description(parts, {**base, "hair": "h00", "accessory": pid, "accessory_color": "k01"})
        assert "eyes_covered" not in reasons and "face_hidden" not in reasons, (name, reasons)
    # 帽子：头发从帽顶上方穿出来会被拒绝（构造一个比帽子高得多的头发）
    lib = copy.deepcopy(parts)
    tall = ["." * 20 for _ in range(20)]
    for y in range(0, 8):
        tall[y] = "." * 5 + "b" * 10 + "." * 5
    hat = ["." * 20 for _ in range(20)]
    for y in range(4, 8):
        hat[y] = "." * 3 + "b" * 14 + "." * 3
    lib["parts"]["hair"]["h02"] = {"back": None, "front": tall}
    lib["parts"]["accessory"]["a01"] = {"back": None, "front": hat}
    desc = {**base, "hair": "h02", "accessory": "a01", "accessory_color": "k02"}
    assert "hair_over_hat" in av.check_description(lib, desc)
    # 同样的帽子、头发没有穿出来（比帽顶矮）就没问题
    lib["parts"]["hair"]["h02"] = {"back": None, "front": ["." * 20] * 5 + ["." * 4 + "b" * 12 + "." * 4] * 3 + ["." * 20] * 12}
    assert "hair_over_hat" not in av.check_description(lib, desc)


# ------------------------------------------------------------------------------------------------ 名册挑选
def test_pick_roster_12_is_pairwise_distinct_in_at_least_three_parts(parts):
    for seed in range(15):
        roster = av.pick_roster(parts, 12, random.Random(seed))
        assert len(roster) == 12
        for i, a in enumerate(roster):
            assert av.check_description(parts, a) == []
            for b in roster[i + 1 :]:
                assert _distinct_fields(a, b) >= 3, (seed, a, b)
                assert av.description_distance(a, b) >= 3
                assert not (a["skin"] == b["skin"] and a["hair_color"] == b["hair_color"]), (seed, a, b)
        # “任意两个至少 3 项不同”里，面孔本身（脸型 / 发型 / 眼睛 / 嘴 / 配饰）也不会全撞
        assert len({(d["face"], d["hair"], d["eyes"], d["mouth"], d["accessory"]) for d in roster}) == 12


def test_pick_roster_respects_taken_and_is_reproducible(parts):
    table = av.pick_roster(parts, 12, random.Random(1))
    more = av.pick_roster(parts, 5, random.Random(2), taken=table[:9])
    assert len(more) == 5
    for new in more:
        for old in table[:9]:
            assert _distinct_fields(new, old) >= 3
    assert av.pick_roster(parts, 12, random.Random(1)) == table
    assert av.pick_roster(parts, 0, random.Random(1)) == []


def test_make_description_still_works_when_all_twelve_seats_are_taken(parts):
    taken = av.make_roster(12, seed=11, parts=parts)
    for seat in range(1, 13):
        desc = av.make_description(seat, taken, seed=seat, parts=parts)
        assert av.validate_description(desc, parts) == desc
        assert av.check_description(parts, desc) == []
        for other in taken:
            assert _distinct_fields(desc, other) >= 3
            assert not (desc["skin"] == other["skin"] and desc["hair_color"] == other["hair_color"])
    # 只换一个人：把他自己排除在 taken 之外，新头像与其余 11 人都明显不同
    rest = taken[:5] + taken[6:]
    new = av.make_description(6, rest, seed=77, parts=parts)
    assert all(_distinct_fields(new, o) >= 3 for o in rest)
    # 即使传进来的 taken 带着 None / 旧版本里不存在的 id，也不会崩
    assert av.validate_description(av.make_description(1, [None, {"v": 1, "face": "f99"}, *taken[:3]], seed=5, parts=parts), parts)


def test_make_description_and_make_roster_are_seedable(parts):
    taken = av.make_roster(4, seed=1, parts=parts)
    a = av.make_description(5, taken, seed=42, parts=parts)
    assert a == av.make_description(5, taken, seed=42, parts=parts)
    assert a != av.make_description(5, taken, seed=43, parts=parts)
    assert av.make_roster(12, seed=9, parts=parts) == av.make_roster(12, seed=9, parts=parts)
    assert av.make_roster(12, seed=9, parts=parts) != av.make_roster(12, seed=10, parts=parts)
    unseeded = {json.dumps(av.make_description(1, [], parts=parts), sort_keys=True) for _ in range(6)}
    assert len(unseeded) > 1  # 不给 seed 就是真随机
    assert av.make_roster(0, parts=parts) == []
    big = av.make_roster(30, seed=2, parts=parts)
    assert len(big) == 30 and all(av.check_description(parts, d) == [] for d in big)
    # 不给 parts 时读仓库里的素材库
    assert av.validate_description(av.make_roster(2, seed=1)[0])


# ------------------------------------------------------------------------------------------------ validate_description
def _good() -> dict:
    return dict(GOLDEN[0][0])


def test_validate_description_accepts_good_input_and_normalises(parts):
    desc = _good()
    out = av.validate_description(desc)  # 只传一个参数
    assert out == desc and out is not desc
    assert list(out) == ["v", *FIELDS]
    assert av.validate_description(desc, parts) == desc
    assert av.validate_description({**desc, "accessory": "none"})["accessory"] == "a00"
    assert av.validate_description({**desc, "accessory": "a00", "accessory_color": ""})["accessory_color"] == ""
    # 输入不会被修改
    before = copy.deepcopy(desc)
    av.validate_description(desc)
    assert desc == before


@pytest.mark.parametrize("bad", [None, [], "x", 3, ("v", 1)])
def test_validate_description_rejects_non_dict_with_chinese_message(bad):
    with pytest.raises(ValueError) as exc:
        av.validate_description(bad)
    assert CJK.search(str(exc.value)) and "对象" in str(exc.value)


@pytest.mark.parametrize("key", ["v", *FIELDS])
def test_validate_description_rejects_missing_keys(key):
    desc = _good()
    del desc[key]
    with pytest.raises(ValueError) as exc:
        av.validate_description(desc)
    assert CJK.search(str(exc.value)) and key in str(exc.value) and "缺少" in str(exc.value)


@pytest.mark.parametrize("field", FIELDS)
def test_validate_description_rejects_unknown_ids(field):
    for value in ("z99", "f99", "", "nope"):
        with pytest.raises(ValueError) as exc:
            av.validate_description({**_good(), field: value})
        assert CJK.search(str(exc.value)) and field in str(exc.value), (field, value, exc.value)


@pytest.mark.parametrize("version", [0, 2, "1", None, 1.5, True])
def test_validate_description_rejects_wrong_version(version):
    with pytest.raises(ValueError) as exc:
        av.validate_description({**_good(), "v": version})
    assert CJK.search(str(exc.value)) and "版本" in str(exc.value)


def test_validate_description_rejects_extra_keys_and_non_string_values():
    with pytest.raises(ValueError) as exc:
        av.validate_description({**_good(), "extra": 1, "hat": "x"})
    assert CJK.search(str(exc.value)) and "extra" in str(exc.value) and "hat" in str(exc.value)
    for value in (3, None, ["f01"], {"id": "f01"}):
        with pytest.raises(ValueError) as exc:
            av.validate_description({**_good(), "face": value})
        assert CJK.search(str(exc.value)) and "face" in str(exc.value)


# ------------------------------------------------------------------------------------------------ load_parts
def test_load_parts_reads_the_repo_file_and_caches(parts):
    lib = av.load_parts()
    assert lib == parts
    assert av.load_parts() is lib  # 按修改时间缓存
    assert av.load_parts(refresh=True) == lib


def test_load_parts_gives_clear_errors(tmp_path):
    with pytest.raises(FileNotFoundError) as missing:
        av.load_parts(tmp_path / "nope.json")
    assert CJK.search(str(missing.value)) and "avatar_lab.py" in str(missing.value) and "nope.json" in str(missing.value)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError) as bad_json:
        av.load_parts(broken)
    assert CJK.search(str(bad_json.value)) and "JSON" in str(bad_json.value)
    shape = tmp_path / "shape.json"
    shape.write_text(json.dumps({"version": 1, "size": 20, "palettes": {}, "parts": {}}), encoding="utf-8")
    with pytest.raises(ValueError) as bad_shape:
        av.load_parts(shape)
    assert CJK.search(str(bad_shape.value)) and "结构" in str(bad_shape.value)


def test_load_parts_notices_a_changed_file(tmp_path, parts):
    path = tmp_path / "parts.json"
    path.write_text(json.dumps(parts), encoding="utf-8")
    first = av.load_parts(path)
    changed = copy.deepcopy(parts)
    changed["palettes"]["fixed"]["outline"] = "#000000"
    path.write_text(json.dumps(changed, indent=2), encoding="utf-8")  # 文件大小也变了
    assert av.load_parts(path)["palettes"]["fixed"]["outline"] == "#000000"
    assert first["palettes"]["fixed"]["outline"] != "#000000"


def test_validate_parts_reports_each_kind_of_damage(parts):
    lib = copy.deepcopy(parts)
    lib["parts"]["face"]["f00"]["front"][3] = "x" * 19
    lib["parts"]["eyes"]["e00"]["front"][0] = "z" * 20
    lib["palettes"]["skin"]["s00"]["b"] = "red"
    lib["parts"]["accessory"]["a00"] = {"back": None, "front": lib["parts"]["accessory"]["a01"]["front"]}
    problems = "\n".join(av.validate_parts(lib))
    assert "f00" in problems and "e00" in problems and "s00" in problems and "a00" in problems
    assert av.validate_parts([]) and av.validate_parts({"version": 2})


# ------------------------------------------------------------------------------------------------ 生成的确定性与命令行
def test_generation_is_deterministic_and_matches_the_committed_file(lab):
    first = lab.parts_text(lab.build_parts())
    second = lab.parts_text(lab.build_parts())
    assert first == second
    committed = PARTS_FILE.read_bytes().replace(b"\r\n", b"\n")
    assert first.encode("utf-8") == committed, "static/avatar/parts.json 与生成器不一致：请重新运行 `python tools/avatar_lab.py` 并提交"
    assert len(committed) < 1_500_000
    assert lab.part_counts(json.loads(committed)) == lab.part_counts(lab.build_parts())


def test_generation_does_not_depend_on_hash_randomisation():
    for hashseed in ("1", "987654"):
        env = dict(os.environ, PYTHONHASHSEED=hashseed, PYTHONUTF8="1")
        out = subprocess.run([sys.executable, str(LAB_FILE), "--check"], capture_output=True, text=True, env=env, timeout=120, cwd=str(ROOT))
        assert out.returncode == 0, out.stdout + out.stderr


def test_lab_rng_is_a_stable_splitmix(lab):
    # 手算的 splitmix64 参考值：生成器不依赖 Python 的 random 实现，跨平台 / 跨版本逐位一致
    r = lab.Rng(0)
    assert [r._next() for _ in range(3)] == [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4, 0x06C45D188009454F]
    a, b = lab.Rng("x/1"), lab.Rng("x/1")
    assert [a.randrange(1000) for _ in range(20)] == [b.randrange(1000) for _ in range(20)]
    assert lab.Rng("x/1").randrange(10**9) != lab.Rng("x/2").randrange(10**9)


def test_cli_sheet_writes_a_png_and_leaves_parts_json_alone(lab, tmp_path, capsys):
    before = (PARTS_FILE.stat().st_mtime_ns, PARTS_FILE.stat().st_size)
    out = tmp_path / "sheet.png"
    assert lab.main(["--sheet", "12", "--seed", "5", "--out", str(out)]) == 0
    assert (PARTS_FILE.stat().st_mtime_ns, PARTS_FILE.stat().st_size) == before  # 只出样张时不动仓库里的 parts.json
    data = out.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert width > 12 * 20 and height > 2 * 20
    assert data[-12:-8] == b"\x00\x00\x00\x00" and data[-8:-4] == b"IEND"
    pos, idat = 8, b""
    while pos < len(data):  # 解压出像素流，确认是合法 PNG
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        if tag == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        assert zlib.crc32(data[pos + 4 : pos + 8 + length]) & 0xFFFFFFFF == struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0]
        pos += 12 + length
    assert len(zlib.decompress(idat)) == height * (width * 4 + 1)
    printed = capsys.readouterr().out
    assert "素材数量" in printed and "样张" in printed
    # 同一个种子，样张逐字节相同
    again = tmp_path / "again.png"
    lab.main(["--sheet", "12", "--seed", "5", "--out", str(again)])
    assert again.read_bytes() == data


def test_cli_diag_and_rejects_and_gallery_run(lab, tmp_path, capsys):
    assert lab.main(["--diag", "400"]) == 0
    assert "被质量过滤拒绝" in capsys.readouterr().out
    assert lab.main(["--rejects", "8", "--out", str(tmp_path / "rejects.png")]) == 0
    assert (tmp_path / "rejects.png").exists()
    assert lab.main(["--gallery", str(tmp_path / "gal"), "--scale", "3"]) == 0
    assert sorted(p.name for p in (tmp_path / "gal").iterdir()) == [f"gallery-{c}.png" for c in ("accessory", "eyes", "face", "hair", "mouth")]


def test_lab_reexports_the_pure_functions_the_task_asked_for(lab, parts):
    assert lab.compose is av.compose and lab.random_description is av.random_description and lab.pick_roster is av.pick_roster
    assert lab.check_description is av.check_description
    px = lab.compose(parts, GOLDEN[0][0])
    assert len(px) == 20 and len(px[0]) == 20 and len(px[0][0]) == 4


if __name__ == "__main__":  # 打印当前素材库的黄金哈希，更新 GOLDEN 用
    lib = json.loads(PARTS_FILE.read_text(encoding="utf-8"))
    for i, (d, _s, _f) in enumerate(GOLDEN, start=1):
        px = av.compose(lib, av.validate_description(d, lib))
        print(f"{i}: {json.dumps(d, sort_keys=False)}\n   sha256={av.pixels_sha256(px)}\n   fnv={av.pixels_fnv(px)}")
