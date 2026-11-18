#!/usr/bin/env python3
"""Generate the architecture diagrams of the README / docs as SVG (one layout, two languages).

    python tools/make_diagrams.py                      # (re)write docs/images/arch-*.{zh,en}.svg
    python tools/make_diagrams.py --check              # exit 1 when a committed file differs from what this script writes
    python tools/make_diagrams.py --list               # print the files
    python tools/make_diagrams.py --only actor --force # one picture; --force writes it even if text overflows (debugging)

The four pictures: ``overview`` (layers), ``actor`` (the mailbox's decision tree), ``worker`` (one activation) and
``timeline`` (how one message wakes everybody).  They are embedded in README.md / README.en.md and in
docs/architecture.md / docs/architecture.en.md; ``tests/test_docs.py`` checks that the committed files equal what
this script writes and that the numbers they quote (retries, grace period, 60 % / 90 % ...) are the code defaults.

Standard library only.  Every picture is drawn from the same coordinates in Chinese and English; the strings
are given as ``c.L("中文", "English")`` pairs, so the two language variants can never drift apart.  Text inside
boxes is measured with a rough font model and the script refuses to write a picture whose text would overflow
its box (``Canvas.problems``), which keeps the diagrams honest when somebody edits a label.

Look: a light card (readable on GitHub's light *and* dark theme), colour per layer (UI / orchestrator / runtime /
brain / world / LLM), the three agent states in the colours of the UI (💭 blue, ⚡ amber, 💤 grey).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans CJK SC', 'Helvetica Neue', Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

INK, INK2, MUTED, FAINT = "#0f172a", "#334155", "#64748b", "#94a3b8"
BG, BORDER = "#f8fafc", "#e2e8f0"

THEMES: dict[str, dict[str, str]] = {
    "sky":     dict(fill="#f0f9ff", head="#38bdf8", head2="#0284c7", stroke="#bae6fd", ink="#0c4a6e", chip="#e0f2fe", chip_ink="#075985", arrow="#0284c7"),
    "blue":    dict(fill="#eff6ff", head="#60a5fa", head2="#2563eb", stroke="#bfdbfe", ink="#1e3a8a", chip="#dbeafe", chip_ink="#1e40af", arrow="#2563eb"),
    "indigo":  dict(fill="#eef2ff", head="#818cf8", head2="#4f46e5", stroke="#c7d2fe", ink="#312e81", chip="#e0e7ff", chip_ink="#3730a3", arrow="#4f46e5"),
    "violet":  dict(fill="#f5f3ff", head="#a78bfa", head2="#7c3aed", stroke="#ddd6fe", ink="#4c1d95", chip="#ede9fe", chip_ink="#5b21b6", arrow="#7c3aed"),
    "emerald": dict(fill="#ecfdf5", head="#34d399", head2="#059669", stroke="#a7f3d0", ink="#064e3b", chip="#d1fae5", chip_ink="#065f46", arrow="#059669"),
    "amber":   dict(fill="#fffbeb", head="#fbbf24", head2="#d97706", stroke="#fde68a", ink="#78350f", chip="#fef3c7", chip_ink="#92400e", arrow="#d97706"),
    "rose":    dict(fill="#fff1f2", head="#fb7185", head2="#e11d48", stroke="#fecdd3", ink="#881337", chip="#ffe4e6", chip_ink="#9f1239", arrow="#e11d48"),
    "slate":   dict(fill="#f8fafc", head="#94a3b8", head2="#475569", stroke="#cbd5e1", ink="#1e293b", chip="#e2e8f0", chip_ink="#334155", arrow="#64748b"),
    "white":   dict(fill="#ffffff", head="#94a3b8", head2="#475569", stroke="#cbd5e1", ink="#1e293b", chip="#f1f5f9", chip_ink="#334155", arrow="#64748b"),
}
ARROW_COLORS = sorted({t["arrow"] for t in THEMES.values()} | {INK2, MUTED})


# ------------------------------------------------------------------------------------------------------ text model
def _char_w(ch: str, size: float, mono: bool, bold: bool) -> float:
    o = ord(ch)
    if o >= 0x2E80 or 0xFF00 <= o <= 0xFFEF:  # CJK, full-width forms
        return size * 1.0
    if o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF:  # emoji / symbols
        return size * 1.15
    if mono:
        return size * 0.61
    if ch in "ilI.,:;|!'`":
        w = 0.30
    elif ch in "ftrj()[]{}/\\-":
        w = 0.38
    elif ch == " ":
        w = 0.30
    elif ch in "mwMW@%":
        w = 0.88
    elif ch.isupper() or ch.isdigit():
        w = 0.62
    else:
        w = 0.54
    return size * w * (1.06 if bold else 1.0)


def _segments(s: str) -> list[tuple[str, bool]]:
    """Split ``a `code` b`` into [(a, False), (code, True), (b, False)]."""
    out: list[tuple[str, bool]] = []
    for i, part in enumerate(s.split("`")):
        if part:
            out.append((part, i % 2 == 1))
    return out


def text_width(s: str, size: float = 14, *, mono: bool = False, bold: bool = False) -> float:
    return sum(sum(_char_w(c, size * (0.94 if code else 1.0), mono or code, bold) for c in seg) for seg, code in _segments(s))


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class Canvas:
    def __init__(self, name: str, lang: str, width: int, height: int, title: str, desc: str) -> None:
        self.name, self.lang, self.w, self.h = name, lang, width, height
        self.title, self.desc = title, desc
        self.parts: list[str] = []
        self.problems: list[str] = []
        self._uid = 0

    # ------------------------------------------------------------------ i18n / output
    def L(self, zh: str, en: str) -> str:
        return zh if self.lang == "zh" else en

    def add(self, s: str) -> None:
        self.parts.append(s)

    def svg(self) -> str:
        self.h = int(-(-self.h // 1))  # a whole number of pixels
        defs = [
            '<defs>',
            '<filter id="sh" x="-6%" y="-6%" width="112%" height="120%"><feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#0f172a" flood-opacity="0.10"/></filter>',
            '<filter id="sh2" x="-10%" y="-10%" width="120%" height="140%"><feDropShadow dx="0" dy="1" stdDeviation="1.5" flood-color="#0f172a" flood-opacity="0.12"/></filter>',
            '<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="1" fill="#dbe3ee"/></pattern>',
        ]
        for name, t in THEMES.items():
            defs.append(
                f'<linearGradient id="g-{name}" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{t["head"]}"/><stop offset="1" stop-color="{t["head2"]}"/></linearGradient>'
            )
        for color in ARROW_COLORS:
            cid = color.lstrip("#")
            defs.append(
                f'<marker id="ah-{cid}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
                f'<path d="M0.5 0.8 L9.2 5 L0.5 9.2 L2.6 5 Z" fill="{color}"/></marker>'
            )
        defs.append("</defs>")
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" '
            f'role="img" aria-labelledby="t d" font-family="{esc(FONT)}">'
            f"<title id=\"t\">{esc(self.title)}</title><desc id=\"d\">{esc(self.desc)}</desc>"
            "<!-- generated by tools/make_diagrams.py - edit the script, not this file -->"
        )
        bg = (
            f'<rect x="0.75" y="0.75" width="{self.w - 1.5}" height="{self.h - 1.5}" rx="18" fill="{BG}" stroke="{BORDER}" stroke-width="1.5"/>'
            f'<rect x="0.75" y="0.75" width="{self.w - 1.5}" height="{self.h - 1.5}" rx="18" fill="url(#dots)" opacity="0.7"/>'
        )
        return head + "".join(defs) + bg + "".join(self.parts) + "</svg>\n"

    # ------------------------------------------------------------------ primitives
    def rect(self, x, y, w, h, *, rx=10, fill="#fff", stroke=None, sw=1.5, shadow=False, dash=None, opacity=None, extra="") -> None:
        a = f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{rx:g}" fill="{fill}"'
        if stroke:
            a += f' stroke="{stroke}" stroke-width="{sw:g}"'
        if dash:
            a += f' stroke-dasharray="{dash}"'
        if shadow:
            a += ' filter="url(#sh)"'
        if opacity is not None:
            a += f' opacity="{opacity}"'
        self.add(a + f" {extra}/>")

    def line(self, x1, y1, x2, y2, *, color=FAINT, sw=1.2, dash=None) -> None:
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" stroke="{color}" stroke-width="{sw:g}"{d}/>')

    def text(self, x, y, s, *, size=14, weight=400, fill=INK2, anchor="start", mono=False, max_w=None, opacity=None, italic=False, rotate=None) -> float:
        """Draw one line (``code`` fragments in backticks use the monospace face).  Returns the estimated width."""
        bold = weight >= 600
        w = text_width(s, size, mono=mono, bold=bold)
        if max_w is not None and w > max_w:
            shrunk = max(size * 0.84, size * max_w / w)
            w2 = text_width(s, shrunk, mono=mono, bold=bold)
            if w2 > max_w + 0.5:
                self.problems.append(f"{self.name}/{self.lang}: text too wide ({w2:.0f} > {max_w:.0f}): {s!r}")
            size, w = shrunk, w2
        spans = []
        for seg, code in _segments(s):
            if code and not mono:
                spans.append(f'<tspan font-family="{esc(MONO)}" font-size="{size * 0.94:.2f}" fill="{fill if fill != INK2 else "#4338ca"}">{esc(seg)}</tspan>')
            else:
                spans.append(esc(seg))
        a = f'<text x="{x:g}" y="{y:g}" font-size="{size:g}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"'
        if mono:
            a += f' font-family="{esc(MONO)}"'
        if italic:
            a += ' font-style="italic"'
        if opacity is not None:
            a += f' opacity="{opacity}"'
        if rotate is not None:
            a += f' transform="rotate({rotate} {x:g} {y:g})"'
        self.add(a + ">" + "".join(spans) + "</text>")
        return w

    def para(self, x, y, lines, *, size=13.5, lh=None, fill=INK2, weight=400, max_w=None, anchor="start") -> float:
        """Several lines, top baseline ``y``; returns the y of the next line."""
        lh = lh or size * 1.45
        for ln in lines:
            self.text(x, y, ln, size=size, fill=fill, weight=weight, max_w=max_w, anchor=anchor)
            y += lh
        return y

    # ------------------------------------------------------------------ composite pieces
    def card(self, x, y, w, h, title, theme="slate", *, sub=None, hh=32, shadow=True, dash=None, title_size=15) -> float:
        """A card with a coloured title band.  Returns the y where the content starts."""
        t = THEMES[theme]
        self.rect(x, y, w, h, rx=14, fill=t["fill"], stroke=t["stroke"], sw=1.5, shadow=shadow, dash=dash)
        self.add(
            f'<path d="M{x:g} {y + hh:g} V{y + 14:g} Q{x:g} {y:g} {x + 14:g} {y:g} H{x + w - 14:g} Q{x + w:g} {y:g} {x + w:g} {y + 14:g} V{y + hh:g} Z" fill="url(#g-{theme})"/>'
        )
        tw = self.text(x + 16, y + hh / 2 + title_size * 0.36, title, size=title_size, weight=700, fill="#ffffff", max_w=w - 32 - (text_width(sub, 12) + 12 if sub else 0))
        if sub:
            self.text(x + w - 14, y + hh / 2 + 4.2, sub, size=12, fill="#ffffffd9", anchor="end", max_w=w - 40 - tw)
        return y + hh

    def chip(self, x, y, label, theme="slate", *, size=12.5, mono=False, h=24, pad=10, bold=False, outline=False) -> float:
        t = THEMES[theme]
        w = text_width(label, size, mono=mono, bold=bold) + 2 * pad
        fill = "#ffffff" if outline else t["chip"]
        self.rect(x, y, w, h, rx=h / 2, fill=fill, stroke=t["stroke"] if outline else None, sw=1.2)
        self.text(x + pad, y + h / 2 + size * 0.34, label, size=size, mono=mono, fill=t["chip_ink"], weight=600 if bold else 500)
        return w

    def chips(self, x, y, labels, theme="slate", *, gap=8, max_x=None, **kw) -> float:
        for lab in labels:
            w = self.chip(x, y, lab, theme, **kw)
            x += w + gap
            if max_x is not None and x - gap > max_x + 0.5:
                self.problems.append(f"{self.name}/{self.lang}: chip row exceeds {max_x}: {lab!r} ends at {x - gap:.0f}")
        return x - gap

    def label(self, x, y, s, *, size=12, fill=INK2, anchor="middle", bg="#ffffff", border=None, weight=500, pad=7) -> float:
        w = text_width(s, size, bold=weight >= 600) + 2 * pad
        h = size + 9
        x0 = x - w / 2 if anchor == "middle" else (x if anchor == "start" else x - w)
        self.rect(x0, y - h / 2, w, h, rx=h / 2, fill=bg, stroke=border, sw=1, opacity=0.96)
        self.text(x0 + pad, y + size * 0.35, s, size=size, fill=fill, weight=weight)
        return w

    def label_lines(self, x, y, lines, *, size=11.5, fill=INK2, bg="#ffffff", border=None, weight=600, pad=7, lh=None) -> None:
        """A pill with several centred lines, centred on (x, y) - used where a long line would not fit."""
        lh = lh or size * 1.35
        w = max(text_width(ln, size, bold=weight >= 600) for ln in lines) + 2 * pad
        h = lh * len(lines) + 8
        self.rect(x - w / 2, y - h / 2, w, h, rx=min(12, h / 2), fill=bg, stroke=border, sw=1, opacity=0.97)
        for i, ln in enumerate(lines):
            self.text(x, y - h / 2 + 4 + lh * (i + 0.78), ln, size=size, fill=fill, weight=weight, anchor="middle")

    def arrow(self, pts, *, color=MUTED, sw=2.0, dash=None, head=True, tail=False, r=10) -> None:
        """Polyline with rounded corners and an arrow head at the end (and optionally at the start)."""
        d = [f"M{pts[0][0]:g} {pts[0][1]:g}"]
        for i in range(1, len(pts) - 1):
            (x0, y0), (x1, y1), (x2, y2) = pts[i - 1], pts[i], pts[i + 1]
            l1 = max(abs(x1 - x0), abs(y1 - y0))
            l2 = max(abs(x2 - x1), abs(y2 - y1))
            rr = min(r, l1 / 2, l2 / 2)
            ax, ay = x1 - (rr if x1 > x0 else -rr if x1 < x0 else 0), y1 - (rr if y1 > y0 else -rr if y1 < y0 else 0)
            bx, by = x1 + (rr if x2 > x1 else -rr if x2 < x1 else 0), y1 + (rr if y2 > y1 else -rr if y2 < y1 else 0)
            d.append(f"L{ax:g} {ay:g} Q{x1:g} {y1:g} {bx:g} {by:g}")
        d.append(f"L{pts[-1][0]:g} {pts[-1][1]:g}")
        cid = color.lstrip("#")
        a = f'<path d="{" ".join(d)}" fill="none" stroke="{color}" stroke-width="{sw:g}" stroke-linecap="round" stroke-linejoin="round"'
        if dash:
            a += f' stroke-dasharray="{dash}"'
        if head:
            a += f' marker-end="url(#ah-{cid})"'
        if tail:
            a += f' marker-start="url(#ah-{cid})"'
        self.add(a + "/>")

    # ------------------------------------------------------------------ wrapping / flow-chart nodes
    @staticmethod
    def _tokens(s: str) -> list[str]:
        """Break points: every CJK character, every space-separated word, `code` stays in one piece."""
        out: list[str] = []
        for seg, code in _segments(s):
            if code:
                out.append("`" + seg + "`")
                continue
            word = ""
            for ch in seg:
                if ord(ch) >= 0x2E80 or 0xFF00 <= ord(ch) <= 0xFFEF:
                    if word:
                        out.append(word)
                        word = ""
                    out.append(ch)
                elif ch == " ":
                    if word:
                        out.append(word)
                        word = ""
                    out.append(" ")
                else:
                    word += ch
            if word:
                out.append(word)
        return out

    def wrap(self, s: str, size: float, max_w: float) -> list[str]:
        lines: list[str] = []
        cur = ""
        for tok in self._tokens(s):
            trial = cur + tok
            if cur and text_width(trial.rstrip(), size) > max_w:
                lines.append(cur.rstrip())
                cur = "" if tok == " " else tok
            else:
                cur = trial
        if cur.strip():
            lines.append(cur.rstrip())
        # do not start a line with a closing punctuation mark, do not end one with an opening bracket
        for i in range(1, len(lines)):
            while lines[i] and lines[i][0] in "，。、；：）)」』！？,.;:":
                lines[i - 1] += lines[i][0]
                lines[i] = lines[i][1:]
            while lines[i - 1] and lines[i - 1][-1] in "（(「『":
                lines[i] = lines[i - 1][-1] + lines[i]
                lines[i - 1] = lines[i - 1][:-1]
        return [ln for ln in lines if ln]

    def wheight(self, s: str, w: float, size: float = 12.5, lh: float | None = None) -> float:
        return len(self.wrap(s, size, w)) * (lh or size * 1.5)

    def wtext(self, x, y, s, *, w, size=12.5, lh=None, fill=MUTED, weight=400, anchor="start") -> float:
        """Wrapped paragraph; returns the y just below it."""
        lh = lh or size * 1.5
        for ln in self.wrap(s, size, w):
            self.text(x if anchor == "start" else x + w / 2, y, ln, size=size, fill=fill, weight=weight, anchor=anchor)
            y += lh
        return y

    def box(self, x, y, w, h, title, theme="slate", *, body=None, size=12.5, tsize=13, pill=False, shadow=True, align="left", dash=None) -> float:
        """A white node with a coloured outline; ``title`` bold, optional wrapped ``body`` underneath.
        ``h=None`` sizes the box to its text (so a longer translation never overflows).  Returns the height."""
        t = THEMES[theme]
        tl = self.wrap(title, tsize, w - 28) if not pill else [title]
        bl = self.wrap(body, size, w - 28) if body else []
        need = 14 + len(tl) * tsize * 1.4 + (len(bl) * size * 1.5 + 2 if bl else 0) + 12
        if h is None:
            h = need
        elif need > h + 0.5 and not pill:
            self.problems.append(f"{self.name}/{self.lang}: box {title!r} needs {need:.0f}px but has {h:.0f}")
        self.rect(x, y, w, h, rx=h / 2 if pill else 11, fill="#ffffff", stroke=t["head2"], sw=1.6, shadow=shadow, dash=dash)
        anchor, tx = ("middle", x + w / 2) if align == "center" else ("start", x + 14)
        if bl:
            ty = y + 14 + tsize * 0.95
        else:
            ty = y + h / 2 + tsize * 0.36 - tsize * 0.7 * (len(tl) - 1)
        for i, ln in enumerate(tl):
            self.text(tx, ty + i * tsize * 1.4, ln, size=tsize, weight=700, fill=t["ink"], anchor=anchor, max_w=w - 20)
        if bl:
            by = ty + (len(tl) - 1) * tsize * 1.4 + tsize * 1.5
            for j, ln in enumerate(bl):
                self.text(x + 14, by + j * size * 1.5, ln, size=size, fill=MUTED)
        return h

    def diamond(self, cx, cy, w, h, lines, theme="amber", *, size=12.5) -> None:
        t = THEMES[theme]
        pts = f"{cx:g},{cy - h / 2:g} {cx + w / 2:g},{cy:g} {cx:g},{cy + h / 2:g} {cx - w / 2:g},{cy:g}"
        self.add(f'<polygon points="{pts}" fill="#ffffff" stroke="{t["head2"]}" stroke-width="1.6" stroke-linejoin="round" filter="url(#sh)"/>')
        n = len(lines)
        for i, ln in enumerate(lines):
            self.text(cx, cy + size * 0.36 + (i - (n - 1) / 2) * size * 1.35, ln, size=size, weight=700, fill=t["ink"], anchor="middle", max_w=w * 0.72)

    def title_block(self, title: str, sub: str) -> None:
        self.add(f'<circle cx="34" cy="36" r="7" fill="url(#g-indigo)"/>')
        self.text(50, 42, title, size=21, weight=800, fill=INK, max_w=self.w - 80)
        self.text(34, 66, sub, size=13.5, fill=MUTED, max_w=self.w - 68)

    def legend(self, x, y, items) -> None:
        """items: (kind, label): kind in {'call','event','data'}"""
        for kind, label in items:
            color = {"call": INK2, "event": "#4f46e5", "data": "#059669", "ui": "#0284c7"}.get(kind, MUTED)
            dash = "6 5" if kind in ("event", "ui") else None
            self.arrow([(x, y), (x + 38, y)], color=color, dash=dash, sw=2)
            x += 48
            x += self.text(x, y + 4.5, label, size=12.5, fill=MUTED) + 26

# ===================================================================================================== diagrams
Diagram = tuple[str, int, int]  # name, width, height


def agent_pill(c: Canvas, x: float, y: float, name: str, *, w: float = 96, h: float = 42) -> None:
    """One agent = an Actor half and a Worker half."""
    t = THEMES["indigo"]
    c.rect(x, y, w, h, rx=10, fill="#ffffff", stroke=t["stroke"], sw=1.2)
    nw = w * (0.54 if w > 100 else 0.46)
    c.rect(x, y, nw, h, rx=10, fill=t["chip"])
    c.rect(x + nw - 10, y, 10, h, rx=0, fill=t["chip"])
    c.text(x + nw / 2, y + h / 2 + 4.4, name, size=12, weight=700, fill=t["chip_ink"], anchor="middle", max_w=nw - 6)
    c.text(x + nw + 8, y + 17, "Actor", size=10.5, weight=700, fill="#4f46e5")
    c.text(x + nw + 8, y + 32, "Worker", size=10.5, weight=700, fill="#7c3aed")


def overview(c: Canvas) -> None:
    c.title_block(
        c.L("Arena-Agents · 整体架构", "Arena-Agents · overall architecture"),
        c.L("引擎只提供与游戏无关的物理层；规则写在剧本里，由 LLM 主持人驱动整局。", "The engine only provides a game-agnostic physical layer; the rules live in the scenario and an LLM moderator drives the game."),
    )
    X, W = 108, 664  # main column
    SX, SW = 800, 216  # side column
    R = X + W
    gap = 38
    y1, h1 = 92, 74
    y2, h2 = y1 + h1 + gap, 74
    y3, h3 = y2 + h2 + gap, 96
    y4, h4 = y3 + h3 + gap, 170
    y5, h5 = y4 + h4 + gap, 84
    y6, h6 = y5 + h5 + gap, 116
    mx = X + W / 2 - 60  # x of the vertical arrows between the layers

    # ---- Row 1: browser
    c.card(X, y1, W, h1, c.L("浏览器 · 单页观战界面", "Browser · single-page spectator UI"), "sky", sub="static/")
    c.chips(X + 16, y1 + 40, [c.L("参与者栏", "Participants"), c.L("频道栏", "Channels"), c.L("信息流 + 请求卡片", "Feed + request cards"), c.L("玩家详情", "Player details"), c.L("上限 · 价格 · 名册", "Limit · Prices · Roster")], "sky", max_x=R - 16)
    # ---- Row 2: API
    c.card(X, y2, W, h2, "FastAPI · app/api", "blue")
    c.chips(X + 16, y2 + 40, ["/api/state", "/api/events", "/api/control/*", "/api/roster*", "/api/budget", "/api/pricing/*"], "blue", mono=True, size=11.5, pad=8, gap=6, max_x=R - 16)
    # ---- Row 3: orchestrator
    c.card(X, y3, W, h3, "GameOrchestrator · app/game", "slate")
    c.chips(X + 16, y3 + 40, [c.L("开始 / 暂停 / 继续", "start / pause / resume"), c.L("按局预算", "per-game budget"), c.L("名册 · 复盘 · 完善", "roster · review · refine"), c.L("存档 v4 · 自动保存", "save v4 · autosave")], "slate", max_x=R - 16)
    c.chips(X + 16, y3 + 68, [c.L("工作区切换", "workspace switch"), c.L("模型绑定", "model bindings"), c.L("演示机器人", "demo bots")], "slate", max_x=R - 16)
    # ---- Row 4: runtime
    c.card(X, y4, W, h4, c.L("Runtime · app/runtime（asyncio）", "Runtime · app/runtime (asyncio)"), "indigo", sub=c.L("13 个 Agent = Actor + Worker", "13 agents = Actor + Worker"))
    c.rect(X + 14, y4 + 44, W - 28, 28, rx=14, fill="#ffffff", stroke=THEMES["indigo"]["stroke"], sw=1.2)
    c.text(X + 30, y4 + 63, "Supervisor", size=13, weight=700, fill=THEMES["indigo"]["ink"])
    c.text(X + 116, y4 + 63, c.L("泵（世界 → agent / 事件总线）· tick 计时 · 看门狗 · 指标", "pump (world → agents / event bus) · tick timers · watchdog · metrics"), size=12, fill=MUTED, max_w=W - 150)
    px = X + 14
    for label in [c.L("主持人", "Moderator"), "N1", "N2", "N3", "…", "N12"]:
        if label == "…":
            c.text(px + 8, y4 + 108, "· · ·", size=14, weight=700, fill=FAINT)
            px += 46
            continue
        wide = label in ("主持人", "Moderator")
        agent_pill(c, px, y4 + 82, label, w=116 if wide else 92)
        px += (116 if wide else 92) + 10
    c.chips(X + 14, y4 + 134, ["ConversationBook", "LlmGate", "UsageBook", "TraceLog", "EventBus"], "indigo", size=11.5, pad=9, gap=6, max_x=R - 14)
    # ---- Row 5: brain
    c.card(X, y5, W, h5, "Brain · app/agents", "violet", sub=c.L("提示词 · 工具 · 上下文", "prompts · tools · context"))
    c.chips(X + 16, y5 + 42, ["system prompt + tool schemas", c.L("FULL / DELTA 消息", "FULL / DELTA messages"), c.L("工具执行 → Intent", "tool execution → Intent"), c.L("笔记 · 压缩", "notes · compaction")], "violet", size=11.8, pad=9, gap=6, max_x=R - 16)
    # ---- Row 6: world
    c.card(X, y6, W, h6, "World · app/world", "emerald", sub=c.L("同步 · 单线程 · 与游戏无关", "synchronous · single-threaded · game-agnostic"))
    c.chips(X + 16, y6 + 44, [c.L("提交闸门 Commit gate", "Commit gate"), c.L("账本 Ledger：只追加", "Ledger: append-only"), c.L("Envelope 投递", "Envelope deliveries")], "emerald", max_x=R - 16)
    c.chips(X + 16, y6 + 76, [c.L("频道 · 发言权 · 请求", "channels · floors · requests"), c.L("能力 · 质询 · 随机数 · 游戏时钟", "abilities · challenges · RNG · game clock")], "emerald", max_x=R - 16)

    # ---- side cards (aligned with the layers they belong to)
    ws_h = y3 + h3 - y2 + 18
    c.card(SX, y2, SW, ws_h, c.L("工作区 .arena/", "Workspace .arena/"), "slate", dash="6 4")
    yy = y2 + 52
    for it in ["providers.v1.enc", "settings.json", "roster/", "sessions/*.enc", "logs/trace-*.jsonl", "pricing.v1.json"]:
        c.text(SX + 18, yy, it, size=12, mono=True, fill=INK2, max_w=SW - 36)
        yy += 20
    c.text(SX + 18, yy + 14, c.L("密钥与存档：加密", "keys + saves: encrypted"), size=11.5, fill=MUTED, max_w=SW - 34)
    c.text(SX + 18, yy + 30, c.L("名册与价格表：明文", "roster + prices: plain"), size=11.5, fill=MUTED, max_w=SW - 34)

    llm_h = y5 + h5 - y4
    c.card(SX, y4, SW, llm_h, c.L("LLM 适配器 · app/llm", "LLM adapters · app/llm"), "amber")
    yy = y4 + 46
    for name in ["OpenAI Chat", "OpenAI Responses", "Anthropic Messages"]:
        c.rect(SX + 14, yy, SW - 28, 26, rx=9, fill="#ffffff", stroke=THEMES["amber"]["stroke"], sw=1.2)
        c.text(SX + 28, yy + 17.5, name, size=12.5, weight=600, fill=THEMES["amber"]["ink"])
        yy += 32
    c.para(SX + 16, yy + 12, [c.L("四类 token + 价格快照", "4 token kinds + price snapshot"), c.L("缓存断点 · 重试 · 端点规划", "cache breakpoints · retries")], size=11.8, lh=17, fill=MUTED, max_w=SW - 30)
    by = yy + 66
    c.arrow([(SX + SW / 2, by - 12), (SX + SW / 2, by + 8)], color="#d97706")
    c.rect(SX + 14, by + 12, SW - 28, 64, rx=10, fill="#ffffff", stroke=THEMES["amber"]["stroke"], sw=1.2)
    c.text(SX + SW / 2, by + 30, "Providers", size=12.5, weight=700, fill=THEMES["amber"]["ink"], anchor="middle")
    c.chips(SX + 22, by + 40, ["DeepSeek", "OpenAI", "Claude"], "amber", size=10.8, pad=6, gap=4, h=22, max_x=SX + SW - 20)

    c.card(SX, y6, SW, h6, c.L("剧本 scenarios/*.md", "Scenario scenarios/*.md"), "emerald")
    c.para(SX + 16, y6 + 56, [c.L("规则 · 身份池 · 模板", "rules · identity pool · templates"), c.L("能力 · 检查项", "abilities · checks"), c.L("主持人手册", "moderator manual")], size=11.8, lh=18, fill=MUTED, max_w=SW - 30)

    # ---- arrows between the layers
    c.arrow([(mx, y1 + h1), (mx, y2)], color="#0284c7", tail=True)
    c.label(mx + 20, y1 + h1 + gap / 2, c.L("REST 控制 · SSE v2 增量事件（带 id，可续传）", "REST control · SSE v2 numbered events (resumable)"), size=11.5, fill="#075985", anchor="start")
    c.arrow([(mx, y2 + h2), (mx, y3)], color=INK2)
    c.arrow([(mx, y3 + h3), (mx, y4)], color=INK2)
    c.label(mx + 20, y3 + h3 + gap / 2, c.L("创建世界与 agent · 开始 / 暂停", "builds world + agents · start / pause"), size=11.5, fill=INK2, anchor="start")
    c.arrow([(mx, y4 + h4), (mx, y5)], color="#7c3aed", tail=True)
    c.label(mx + 20, y4 + h4 + gap / 2, "start_message · inject_message · execute_tool", size=11.5, fill="#5b21b6", anchor="start")
    c.arrow([(mx, y5 + h5), (mx, y6)], color="#059669", tail=True)
    c.label(mx + 20, y5 + h5 + gap / 2, c.L("Intent →  ← Receipt（工具结果）", "Intent →  ← Receipt (tool result)"), size=11.5, fill="#065f46", anchor="start")

    # Envelope (world -> runtime) and UiEvent (runtime -> API) along the left margin
    ax = 58
    c.arrow([(X - 2, y6 + 58), (ax, y6 + 58), (ax, y4 + 112), (X - 2, y4 + 112)], color="#4f46e5", dash="6 5")
    c.label_lines(ax, (y6 + 58 + y4 + 112) / 2, ["Envelope", c.L("唤醒 agent", "wakes agents"), c.L("经 Supervisor", "via pump")], fill="#4338ca", border="#c7d2fe", size=11)
    c.arrow([(X - 2, y4 + 62), (ax, y4 + 62), (ax, y2 + 38), (X - 2, y2 + 38)], color="#0284c7", dash="6 5")
    c.label_lines(ax, (y4 + 62 + y2 + 38) / 2, ["UiEvent", c.L("观战事件", "spectator"), "→ SSE"], fill="#075985", border="#bae6fd", size=11)

    # right side links
    c.arrow([(R, y3 + 52), (SX, y3 + 52)], color=MUTED, dash="6 5")
    c.arrow([(R, y4 + 104), (SX, y4 + 104)], color="#d97706")
    c.label((R + SX) / 2, y4 + 88, "LLM", size=11, fill="#92400e", bg="#fffbeb", border="#fde68a")
    c.arrow([(SX, y6 + 58), (R, y6 + 58)], color="#059669")
    c.legend(X, y6 + h6 + 36, [("call", c.L("调用 / 数据", "call / data")), ("event", c.L("通知 / 事件", "notification / event"))])


def yesno(c: Canvas, x: float, y: float, yes: bool) -> None:
    c.label(x, y, c.L("是", "yes") if yes else c.L("否", "no"), size=11.5, fill="#047857" if yes else "#be123c", bg="#ffffff", border="#a7f3d0" if yes else "#fecdd3", weight=700, pad=6)


def actor(c: Canvas) -> None:
    c.title_block(
        c.L("Actor · 永远在线的信箱", "Actor · the always-on mailbox"),
        c.L("`deliver()` 同步、不阻塞：每一条投递都会唤醒 agent；只有 interrupt（带防饥饿保护）才可能取消在途的调用。", "`deliver()` is synchronous and never blocks: every delivered envelope wakes the agent; only an interrupt (with a starvation guard) may cancel work in flight."),
    )
    # ---- left column: what an envelope is, what "gen" and "focus" are (cards sized to their text)
    prio = c.L("`priority`：urgent | normal，只是个标签（写进 trace），不影响唤醒", "`priority`: urgent | normal, only a label (it goes to the trace), it does not affect waking")
    intr = c.L("`interrupt`：相关的在途工作已作废", "`interrupt`: work in flight about it is void")
    gen_t = c.L("每次激活、取消、停止都让 `gen` +1。Worker 在模型调用返回后检查：对不上就丢弃结果，什么都不写进世界。", "Every activation, cancellation and stop bumps `gen`. A Worker checks it when the model call returns: a stale one drops its result and writes nothing.")
    foc_t = c.L("= 本次运行已经读到的频道和请求。interrupt 只在命中焦点时才会取消。", "= the channels and requests this run has read. An interrupt only cancels when it hits the focus.")
    h_env = 32 + 16 + 3 * 16.5 + 8 + c.wheight(prio, 166, 12, 17) + 4 + c.wheight(intr, 166, 12, 17) + 14
    c.card(28, 96, 192, h_env, c.L("Envelope（信封）", "Envelope"), "emerald", sub="")
    c.text(42, 148, "`kind`", size=12, fill=INK2)
    c.text(42, 165, "message · floor · request", size=11.5, mono=True, fill=INK2, max_w=170)
    c.text(42, 181, "challenge · ability · notice", size=11.5, mono=True, fill=INK2, max_w=170)
    y = c.wtext(42, 205, prio, w=166, size=12, fill=INK2, lh=17)
    c.wtext(42, y + 4, intr, w=166, size=12, fill=INK2, lh=17)
    y_gen = 96 + h_env + 16
    h_gen = 32 + 22 + c.wheight(gen_t, 166, 12, 17) + 12
    c.card(28, y_gen, 192, h_gen, c.L("gen（代数）", "gen (generation)"), "slate")
    c.wtext(42, y_gen + 54, gen_t, w=166, size=12, fill=INK2, lh=17)
    y_foc = y_gen + h_gen + 16
    h_foc = 32 + 22 + c.wheight(foc_t, 166, 12, 17) + 12
    c.card(28, y_foc, 192, h_foc, c.L("焦点（focus）", "focus"), "slate")
    c.wtext(42, y_foc + 54, foc_t, w=166, size=12, fill=INK2, lh=17)
    left_bottom = y_foc + h_foc

    # ---- the decision tree
    cx = 640
    c.arrow([(220, 130), (cx - 104, 130)], color="#059669")
    c.label(330, 130, "Supervisor.pump()", size=11.5, fill="#065f46", bg="#ecfdf5", border="#a7f3d0")
    c.box(cx - 104, 108, 208, 44, "agent.deliver(envelope)", "indigo", pill=True, align="center", tsize=13)
    c.arrow([(cx, 152), (cx, 172)], color="#4f46e5")
    c.box(cx - 104, 172, 208, 46, c.L("追加到 pending", "append to pending"), "indigo", align="center", tsize=13)
    c.arrow([(cx, 218), (cx, 240)], color="#4f46e5")
    c.diamond(cx, 284, 190, 88, c.L(["有 Worker", "在运行吗？"], ["Is a Worker", "running?"]), "amber")

    # left branch: sleeping
    lx = 380
    c.arrow([(cx - 95, 284), (lx, 284), (lx, 372)], color="#4f46e5")
    yesno(c, lx + 52, 284, False)
    c.text(lx - 6, 330, c.L("💤 休眠中", "💤 sleeping"), size=12, weight=700, fill="#475569", anchor="end")
    h2 = c.box(lx - 98, 372, 196, None, "call_soon", "indigo", body=c.L("安排一次激活：同一次泵里投递的合成一次；不等待，没有定时器", "schedules ONE activation: all envelopes of one pump are one activation; no wait, no timer"), size=12)
    c.arrow([(lx, 372 + h2), (lx, 372 + h2 + 26)], color="#4f46e5")
    c.box(lx - 98, 372 + h2 + 26, 196, None, c.L("启动 Worker（gen +1）", "start a Worker (gen +1)"), "indigo", body=c.L("取走全部 pending 作为唤醒原因 → 💭 思考", "takes everything pending as the wake reasons → 💭 thinking"), size=12)

    # right branch: running
    rx = 870
    c.arrow([(cx + 95, 284), (rx, 284), (rx, 340)], color="#4f46e5")
    yesno(c, rx - 52, 284, True)
    c.text(rx + 8, 322, c.L("💭 / ⚡ 运行中", "💭 / ⚡ running"), size=12, weight=700, fill="#475569")
    c.diamond(rx, 392, 214, 104, c.L(["interrupt 且命中", "本次运行的焦点？"], ["interrupt that hits", "the run's focus?"]), "amber")
    # no -> merge
    mx = 612
    c.arrow([(rx - 107, 392), (mx + 98, 392)], color="#4f46e5")
    yesno(c, (rx - 107 + mx + 98) / 2, 392, False)
    c.box(mx - 98, 392 - 52, 196, None, c.L("到下一轮边界合并", "merge at the next round boundary"), "emerald", body=c.L("期间到达的信封合成一条 DELTA 注入对话，不额外等待", "what arrived meanwhile becomes ONE DELTA message; no extra wait"), size=12)
    # yes -> cancel
    c.arrow([(rx, 444), (rx, 500)], color="#e11d48")
    yesno(c, rx + 30, 472, True)
    h5 = c.box(rx - 120, 500, 240, None, c.L("取消在途调用", "cancel the call in flight"), "rose", body=c.L("`gen` +1，过期结果丢弃；Worker 退场（最多等 `cancel_grace_s` = 5 秒）后带着全部 pending 重新激活", "`gen` +1, the stale result is dropped; once the Worker has unwound (at most `cancel_grace_s` = 5 s) the agent is re-activated with everything pending"), size=12)
    c.wtext(rx - 120, 500 + h5 + 22, c.L("防饥饿：连续被取消 2 次后，下一次 interrupt 改为合并（`starvation_limit`）。", "Starvation guard: after 2 cancellations in a row the next interrupt is merged instead (`starvation_limit`)."), w=250, size=11.5, fill="#9f1239", lh=16)

    # ---- bottom: three side paths (cards sized to their text)
    cw = 316
    alarm_t = c.L("Supervisor 每个 tick（`tick_interval` 0.1 秒）调用 `timer_due(now)`（游戏时钟，暂停时冻结），到点就 `fire_timer` 合成 `timer.elapsed` 信封，再走上面的 `deliver()`。新信封到达会作废闹钟；若这次激活什么也没发生，闹钟恢复。",
                  "Every supervisor tick (`tick_interval` 0.1 s) calls `timer_due(now)` (game clock, frozen while paused); when due, `fire_timer` makes a `timer.elapsed` envelope and sends it through the `deliver()` above. Any arrival voids the alarm; if the activation turns out to be a no-op the alarm is restored.")
    retry_t = c.L("Worker 抛异常：对话原样不动，指数退避后重试（1 秒、2 秒……最多重试 `llm_retries` = 2 次）；仍失败就放弃这次激活并发出 `error` 事件，不牵连其他 agent。",
                  "The Worker raises: the conversation is untouched, retry with exponential backoff (1 s, 2 s … at most `llm_retries` = 2 times); then give up this activation and publish an `error` event - other agents are not affected.")
    stop_t = c.L("`stop()` 同步且幂等：取消 Worker 和所有计时器、丢弃 pending，界面显示 💤。对话留在 ConversationBook 里，继续时沿用；Supervisor 会等被取消的 Worker 真正退场，不泄漏任务。",
                 "`stop()` is synchronous and idempotent: it cancels the Worker and every timer, drops what is pending and shows 💤. The conversation stays in the ConversationBook and a resumed game continues with it; the Supervisor waits until cancelled Workers have really unwound, so nothing leaks.")
    ch = 54 + max(c.wheight(t_, cw - 32, 11.8, 16.5) for t_ in (alarm_t, retry_t, stop_t)) + 14
    yb = max(500 + h5 + 22 + 3 * 16 + 26, left_bottom + 28)
    c.h = yb + ch + 24
    c.card(28, yb, cw, ch, c.L("⏰ 闹钟：sleep(秒) 到点", "⏰ Alarm: sleep(seconds) is due"), "amber")
    c.wtext(44, yb + 54, alarm_t, w=cw - 32, size=11.8, fill=INK2, lh=16.5)
    c.card(28 + cw + 16, yb, cw, ch, c.L("失败与重试", "Failure and retry"), "rose")
    c.wtext(44 + cw + 16, yb + 54, retry_t, w=cw - 32, size=11.8, fill=INK2, lh=16.5)
    c.card(28 + 2 * (cw + 16), yb, cw, ch, c.L("停止 / 暂停", "Stop / pause"), "slate")
    c.wtext(44 + 2 * (cw + 16), yb + 54, stop_t, w=cw - 32, size=11.8, fill=INK2, lh=16.5)


def bar(c: Canvas, x0: float, x1: float, y: float, state: str, label: str = "", *, h: float = 32) -> None:
    """One stretch of an agent's life in the state's colour (💭 thinking · ⚡ acting · 💤 sleeping)."""
    icon, color, fill = {"sleeping": ("💤", "#64748b", "#e2e8f0"), "thinking": ("💭", "#2563eb", "#dbeafe"), "acting": ("⚡", "#d97706", "#fef3c7")}[state]
    c.rect(x0, y, x1 - x0, h, rx=9, fill=fill, stroke=color, sw=1.3)
    w = x1 - x0
    if w >= 30:
        c.text(x0 + 8, y + h / 2 + 4.6, icon, size=13)
    if label and w >= 62:
        c.text(x0 + 28, y + h / 2 + 4.2, label, size=11.5, weight=600, fill=color, max_w=w - 34)


def timeline(c: Canvas) -> None:
    c.title_block(
        c.L("时间线 · 一条消息怎样唤醒所有人（示意）", "Timeline · how one message wakes everybody (illustration)"),
        c.L("自由讨论里的三次提交：休眠的 agent 被唤醒，正在思考的 agent 在下一轮边界合并，出局的玩家不被广播唤醒。", "Three commits in a free discussion: sleeping agents wake up, an agent that is thinking merges at its next round boundary, a player who is out is not woken by broadcasts."),
    )
    LX, LW = 28, 128
    TX0, TX1 = 176, 1012
    lanes = [
        ("world", c.L("World", "World"), c.L("提交闸门 → 账本", "commit gate → ledger"), "emerald"),
        ("mod", c.L("主持人", "Moderator"), "agent", "amber"),
        ("n1", "N1 Alice", "agent", "indigo"),
        ("n2", "N2 Bob", "agent", "indigo"),
        ("n3", "N3 Carol", c.L("已出局", "out"), "slate"),
    ]
    y0, lane_h, gap = 132, 52, 16
    c.arrow([(TX0 - 6, 108), (TX1 + 6, 108)], color=FAINT, sw=1.6)
    c.text(TX1 + 6, 100, c.L("时间", "time"), size=11.5, fill=MUTED, anchor="end")
    ys = {key: y0 + i * (lane_h + gap) for i, (key, *_r) in enumerate(lanes)}
    for key, name, sub, theme in lanes:
        y = ys[key]
        t = THEMES[theme]
        c.rect(TX0 - 6, y, TX1 - TX0 + 12, lane_h, rx=12, fill="#ffffff", stroke=t["stroke"], sw=1.2)
        c.rect(LX, y, LW, lane_h, rx=12, fill=t["chip"], stroke=t["stroke"], sw=1.2)
        c.text(LX + 12, y + (22 if sub != "agent" else 31), name, size=13, weight=700, fill=t["chip_ink"], max_w=LW - 20)
        if sub != "agent":
            c.text(LX + 12, y + 40, sub, size=11, fill=MUTED, max_w=LW - 20)

    # events (x positions) and the commits they stand for
    e = [262, 522, 640]
    names = [c.L("#10 主持人 `announce`", "#10 moderator `announce`"), c.L("#11 N1 `speak`", "#11 N1 `speak`"), c.L("#12 N2 `speak`", "#12 N2 `speak`")]
    wy = ys["world"]
    for x, nm in zip(e, names):
        c.add(f'<circle cx="{x}" cy="{wy + lane_h / 2}" r="7" fill="#059669"/>')
        c.text(x + 12, wy + lane_h / 2 + 4.4, nm, size=11.5, weight=600, fill="#065f46", max_w=170)
        c.line(x, wy + lane_h, x, ys["n3"] + lane_h, color="#a7f3d0", sw=1.4, dash="4 4")

    by = lambda key: ys[key] + (lane_h - 32) / 2
    # moderator: announces, then is woken by every later message but has nothing to add
    bar(c, TX0, e[0] - 36, by("mod"), "sleeping")
    bar(c, e[0] - 36, e[0] + 26, by("mod"), "acting", "")
    bar(c, e[0] + 26, e[1] + 10, by("mod"), "sleeping")
    bar(c, e[1] + 10, e[1] + 78, by("mod"), "thinking", "")
    bar(c, e[1] + 78, e[2] + 8, by("mod"), "sleeping")
    bar(c, e[2] + 8, e[2] + 76, by("mod"), "thinking", "")
    bar(c, e[2] + 76, TX1, by("mod"), "sleeping")
    # N1: woken by the announcement, thinks, speaks, sleeps, woken again by N2
    bar(c, TX0, e[0] + 8, by("n1"), "sleeping")
    bar(c, e[0] + 8, e[1] - 28, by("n1"), "thinking", c.L("思考", "thinking"))
    bar(c, e[1] - 28, e[1] + 30, by("n1"), "acting", "")
    bar(c, e[1] + 30, e[2] + 8, by("n1"), "sleeping")
    bar(c, e[2] + 8, e[2] + 84, by("n1"), "thinking", "")
    bar(c, e[2] + 84, TX1, by("n1"), "sleeping")
    # N2: slower; the message of N1 arrives while it is still thinking
    bar(c, TX0, e[0] + 8, by("n2"), "sleeping")
    bar(c, e[0] + 8, e[2] - 28, by("n2"), "thinking", c.L("思考（第 1 轮）", "thinking (round 1)"))
    bar(c, e[2] - 28, e[2] + 30, by("n2"), "acting", "")
    bar(c, e[2] + 30, e[2] + 150, by("n2"), "thinking", c.L("第 2 轮", "round 2"))
    bar(c, e[2] + 150, TX1, by("n2"), "sleeping")
    # N3: out - broadcasts do not wake it
    bar(c, TX0, TX1, by("n3"), "sleeping")
    c.text(e[2] + 28, by("n3") + 20.5, c.L("出局：只有发给他本人的事件才唤醒", "out: only events addressed to him wake it"), size=11.5, weight=600, fill="#64748b", max_w=TX1 - e[2] - 40)

    # markers on the lanes: wake / merge / not delivered
    def wake(x, key):
        c.add(f'<circle cx="{x}" cy="{ys[key] + lane_h / 2}" r="6.5" fill="#10b981" stroke="#ffffff" stroke-width="2"/>')

    def merge(x, key):
        c.add(f'<circle cx="{x}" cy="{ys[key] + lane_h / 2}" r="7" fill="#ffffff" stroke="#4f46e5" stroke-width="2.4"/><circle cx="{x}" cy="{ys[key] + lane_h / 2}" r="2.4" fill="#4f46e5"/>')

    def blocked(x, key):
        cy = ys[key] + lane_h / 2
        c.add(f'<g stroke="#e11d48" stroke-width="2.6" stroke-linecap="round"><line x1="{x - 5.5}" y1="{cy - 5.5}" x2="{x + 5.5}" y2="{cy + 5.5}"/><line x1="{x - 5.5}" y1="{cy + 5.5}" x2="{x + 5.5}" y2="{cy - 5.5}"/></g>')

    wake(e[0], "n1"); wake(e[0], "n2"); blocked(e[0], "n3")
    wake(e[1], "mod"); merge(e[1], "n2"); blocked(e[1], "n3")
    wake(e[2], "mod"); wake(e[2], "n1"); blocked(e[2], "n3")
    # annotations
    ay = ys["n3"] + lane_h + 22
    lx = TX0 - 6
    c.add(f'<circle cx="{lx + 6}" cy="{ay}" r="6.5" fill="#10b981" stroke="#ffffff" stroke-width="2"/>')
    lx += 20
    lx += c.text(lx, ay + 4.4, c.L("唤醒（休眠 → 思考）", "wakes (sleeping → thinking)"), size=12, fill=MUTED) + 22
    c.add(f'<circle cx="{lx + 6}" cy="{ay}" r="7" fill="#ffffff" stroke="#4f46e5" stroke-width="2.4"/><circle cx="{lx + 6}" cy="{ay}" r="2.4" fill="#4f46e5"/>')
    lx += 20
    lx += c.text(lx, ay + 4.4, c.L("到达时正在运行：进 pending，下一轮边界合并", "arrives mid-run → pending, merged at the next round boundary"), size=12, fill=MUTED) + 22
    c.add(f'<g stroke="#e11d48" stroke-width="2.6" stroke-linecap="round"><line x1="{lx + 1}" y1="{ay - 5.5}" x2="{lx + 12}" y2="{ay + 5.5}"/><line x1="{lx + 1}" y1="{ay + 5.5}" x2="{lx + 12}" y2="{ay - 5.5}"/></g>')
    lx += 20
    c.text(lx, ay + 4.4, c.L("广播不投递给出局的玩家", "not delivered to a player who is out"), size=12, fill=MUTED, max_w=TX1 - lx)

    # three take-aways
    ty = ay + 34
    cw = (TX1 - LX - 32) / 3
    notes = [
        (c.L("每条消息都唤醒", "Every message wakes"), "emerald", c.L("世界提交一条记录，就给每个能看到它的 agent 投递一个 Envelope（发送者本人除外）；同一次泵里的合成一次激活。", "When the world commits an entry it delivers an Envelope to every agent that can see it (the sender excepted); everything of one pump is one activation.")),
        (c.L("运行中不打断", "No interruption mid-run"), "indigo", c.L("N2 还在想的时候 N1 说话了：N2 不被取消，这条进 pending，等它的模型调用返回、工具执行完，在下一轮边界作为 DELTA 注入。", "N1 speaks while N2 is still thinking: N2 is not cancelled; the entry waits in pending and is injected as a DELTA at the next round boundary, after the call returns and the tools ran.")),
        (c.L("正确性靠提交闸门", "The commit gate keeps it correct"), "amber", c.L("agent 的每个动作在提交的那一刻重新校验（发言权、成员、字数、是否过时），所以“想的时候世界变了”不会写出错误的东西。", "Every action is re-validated at the moment it is committed (floor, membership, length, staleness), so a world that changed while the agent was thinking cannot produce a wrong write.")),
    ]
    hh = 0
    for i, (title, theme, body) in enumerate(notes):
        hh = max(hh, 32 + c.wheight(body, cw - 28, 11.8, 16.5) + 24)
    for i, (title, theme, body) in enumerate(notes):
        x = LX + i * (cw + 16)
        c.card(x, ty, cw, hh, title, theme, title_size=14)
        c.wtext(x + 14, ty + 56, body, w=cw - 28, size=11.8, fill=INK2, lh=16.5)
    c.h = ty + hh + 24


def corner_state(c: Canvas, x_right: float, y_top: float, state: str) -> None:
    """A small state pill sitting on the top-right corner of a node (the ``agent.status`` the node publishes)."""
    icon, color, fill = {"sleeping": ("💤", "#475569", "#e2e8f0"), "thinking": ("💭", "#1d4ed8", "#dbeafe"), "acting": ("⚡", "#b45309", "#fef3c7")}[state]
    name = {"sleeping": c.L("休眠", "sleeping"), "thinking": c.L("思考", "thinking"), "acting": c.L("行动", "acting")}[state]
    w = text_width(name, 11.5, bold=True) + 38
    c.rect(x_right - w - 10, y_top - 11, w, 22, rx=11, fill=fill, stroke=color, sw=1.1)
    c.text(x_right - w - 10 + 9, y_top + 4.4, icon, size=12)
    c.text(x_right - w - 10 + 28, y_top + 4.2, name, size=11.5, weight=700, fill=color)


def msg_row(c: Canvas, x: float, y: float, w: float, tag: str, text: str, theme: str, *, lock: bool = False, h: float = 32) -> None:
    t = THEMES[theme]
    c.rect(x, y, w, h, rx=8, fill=t["fill"], stroke=t["head2"], sw=1.2)
    tw = text_width(tag, 11.5, bold=True, mono=True) + 14
    c.rect(x, y, tw, h, rx=8, fill=t["chip"])
    c.rect(x + tw - 8, y, 8, h, rx=0, fill=t["chip"])
    c.text(x + 7, y + h / 2 + 4, tag, size=11.5, weight=700, fill=t["chip_ink"], mono=True)
    c.text(x + tw + 8, y + h / 2 + 4, text, size=11.5, fill=INK2, max_w=w - tw - 36)
    if lock:
        c.text(x + w - 8, y + h / 2 + 4.5, "🔒", size=12, anchor="end")


def worker(c: Canvas) -> None:
    c.title_block(
        c.L("Worker · 一次激活 = 一个 asyncio 任务", "Worker · one activation = one asyncio task"),
        c.L("追加式对话 → 模型 → 工具 → 合并，循环到 agent 调用 `sleep`；没有轮数上限，也不会因为省钱而少被唤醒。", "Append-only conversation → model → tools → merge, looping until the agent calls `sleep`; no round cap, and no fewer wake-ups to save money."),
    )
    X, W = 28, 616  # left column
    RX, RW = 668, 344  # right column
    bx, bw = 74, 330  # main chain
    mcx = bx + bw / 2
    sx, sw_ = 432, 204  # side boxes

    y = 100
    # ---- ① open the conversation (outside the loop)
    h1 = c.box(bx, y, bw, None, c.L("① 打开对话（Brain，同步）", "① Open the conversation (Brain, synchronous)"), "violet",
               body=c.L("`begin_run` 重置本次运行的状态。ConversationBook 里没有这个 agent 的对话 → 创建：`system` + `tools`（此后逐字节不变）+ FULL `start_message`；已经有 → 追加 DELTA `inject_message`，没有新东西可说就不调用模型，直接回到 💤。", "`begin_run` resets the per-run state. No conversation in the ConversationBook yet → create it: `system` + `tools` (byte-stable from now on) + a FULL `start_message`; otherwise append a DELTA `inject_message`, and if there is nothing to tell, make no model call and go back to 💤."), size=11.8)
    c.arrow([(mcx, y + h1), (mcx, y + h1 + 34)], color="#4f46e5")
    y += h1 + 34

    # ---- loop container (drawn first, content on top); height is known after the content is laid out, so draw it last
    loop_top = y
    y += 40
    # ② think
    h2 = c.box(bx, y, bw, None, c.L("② 思考", "② Think"), "indigo",
               body=c.L("`llm.complete([system] + messages, tools)`：整条对话原样发给模型（经 `LlmGate`）。", "`llm.complete([system] + messages, tools)`: the whole conversation goes to the model as it is (through the `LlmGate`)."), size=11.8)
    corner_state(c, bx + bw, y, "thinking")
    y2c = y + h2 / 2
    c.arrow([(mcx, y + h2), (mcx, y + h2 + 26)], color="#4f46e5")
    y += h2 + 26
    # ③ account + stale check
    h3 = c.box(bx, y, bw, None, c.L("③ 记账 · 检查 gen", "③ Account · check gen"), "slate",
               body=c.L("`UsageBook.record_llm`：四类 token + 价格快照 → 预算检查。`gen` 已过期（被取消或停止）→ 丢弃结果，什么也不写。", "`UsageBook.record_llm`: four token kinds + price snapshot → budget check. `gen` is stale (cancelled or stopped) → drop the result, write nothing."), size=11.8)
    c.arrow([(mcx, y + h3), (mcx, y + h3 + 24)], color="#4f46e5")
    y += h3 + 24
    # ◇ tools?
    dh = 92
    dcy = y + dh / 2
    c.diamond(mcx, dcy, 232, dh, c.L(["模型调用了", "工具吗？"], ["Did the model", "call tools?"]), "amber")
    # no -> exit A
    ax_ = mcx + 116
    c.arrow([(ax_, dcy), (sx, dcy)], color="#e11d48")
    yesno(c, (ax_ + sx) / 2, dcy, False)
    hA = c.box(sx, dcy - 52, sw_, None, c.L("只有文字", "Text only"), "rose",
               body=c.L("追加 assistant 文本。被截断 → 提醒一次；否则 `no_tool_nudge` 提醒一次；pending 里的信封一并合并。没有任何可补充的 → 结束本次运行（等同不定时 `sleep`），否则回到 ②。", "Append the assistant text. Cut off → one reminder; otherwise one `no_tool_nudge`; whatever is pending is merged too. Nothing to add → the run ends (same as a `sleep` without timer), otherwise back to ②."), size=11.3)
    c.arrow([(mcx, y + dh), (mcx, y + dh + 30)], color="#4f46e5")
    yesno(c, mcx + 22, y + dh + 15, True)
    y += dh + 30
    # ④ act
    h4 = c.box(bx, y, bw, None, c.L("④ 行动", "④ Act"), "amber",
               body=c.L("按顺序同步执行全部工具（中间没有 `await`，不会被取消在半途），然后一次性追加 assistant 消息 + 全部工具结果。", "Run every tool call in order, synchronously (no `await`, so it can never be cancelled half-way), then append the assistant message + ALL tool results in one stretch."), size=11.8)
    corner_state(c, bx + bw, y, "acting")
    # tools note (right of ④)
    hN = c.box(sx, max(y - 6, dcy - 52 + hA + 14), sw_, None, c.L("工具的去处", "Where tools go"), "amber",
               body=c.L("世界类 → `Intent`（`based_on_seq`）→ 提交闸门 → `Receipt`，作为工具结果；共享类：笔记 · 文档 · `compact_context`；`sleep(seconds?)`：`ends_run`，可带游戏时钟闹钟。", "World tools → `Intent` (`based_on_seq`) → commit gate → `Receipt` as the tool result; shared tools: notes · documents · `compact_context`; `sleep(seconds?)`: `ends_run`, optionally with a game-clock alarm."), size=11.3)
    c.arrow([(mcx, y + h4), (mcx, y + h4 + 26)], color="#4f46e5")
    y = y + h4 + 26
    # ⑤ maintain
    h5_ = c.box(bx, y, bw, None, c.L("⑤ 整理对话", "⑤ Keep the conversation healthy"), "violet",
                body=c.L("调用了 `compact_context` → 重建（丢弃对话，新开一条 FULL，`epoch` +1）；否则检查用量：≥ 60 % 窗口提醒压缩一次，≥ 90 % 裁剪最早的完整轮。", "`compact_context` was called → rebuild (drop the conversation, start a fresh FULL one, `epoch` +1); otherwise check the usage: ≥ 60 % of the window → one reminder to compact, ≥ 90 % → trim the oldest complete rounds."), size=11.8)
    c.arrow([(mcx, y + h5_), (mcx, y + h5_ + 24)], color="#4f46e5")
    y += h5_ + 24
    # ◇ sleep?
    dcy2 = y + dh / 2
    c.diamond(mcx, dcy2, 232, dh, c.L(["`sleep` 结束了", "本次运行吗？"], ["Did `sleep` end", "the run?"]), "amber")
    c.arrow([(ax_, dcy2), (sx, dcy2)], color="#475569")
    yesno(c, (ax_ + sx) / 2, dcy2, True)
    hB = c.box(sx, dcy2 - 56, sw_, None, c.L("结束本次运行", "End of the run"), "slate",
               body=c.L("带 `seconds` → 设定 `sleep_until`（游戏时钟）。若这时又有 pending → 立刻再激活，否则 💤。", "With `seconds` → set `sleep_until` (game clock). If something is pending by now → activate again at once, otherwise 💤."), size=11.3)
    corner_state(c, sx + sw_, dcy2 - 56, "sleeping")
    c.arrow([(mcx, y + dh), (mcx, y + dh + 28)], color="#4f46e5")
    yesno(c, mcx + 20, y + dh + 14, False)
    y += dh + 28
    # ⑥ merge
    h6 = c.box(bx, y, bw, None, c.L("⑥ 合并运行中到达的信封", "⑥ Merge what arrived meanwhile"), "emerald",
               body=c.L("pending 里的信封 → 一条 DELTA（`brain.inject_message`）追加到对话，不额外等待；然后回到 ②。", "The envelopes in pending → ONE DELTA message (`brain.inject_message`) appended to the conversation, no extra wait; then back to ②."), size=11.8)
    y6c = y + h6 / 2
    loop_bottom = y + h6 + 18
    # container + loop-back arrow
    c.rect(X + 8, loop_top, W - 8, loop_bottom - loop_top, rx=16, fill="none", stroke="#a5b4fc", sw=1.6, dash="7 5")
    c.text(X + 24, loop_top + 24, c.L("循环 · 没有轮数上限（直到 `sleep`，或模型不再调用工具）", "Loop · no round cap (until `sleep`, or the model stops calling tools)"), size=12.5, weight=700, fill="#4338ca", max_w=W - 50)
    c.arrow([(bx, y6c), (X + 36, y6c), (X + 36, y2c), (bx, y2c)], color="#4f46e5", dash="6 5")
    ymid = (y6c + y2c) / 2
    lw = text_width(c.L("下一轮", "next round"), 11, bold=True) + 14
    c.rect(X + 36 - 10, ymid - lw / 2, 20, lw, rx=10, fill="#ffffff", stroke="#c7d2fe", sw=1)
    c.text(X + 36 + 4, ymid, c.L("下一轮", "next round"), size=11, weight=600, fill="#4338ca", anchor="middle", rotate=-90)

    # ---- right column
    ry = 100
    rows = [
        ("system", c.L("角色 · 规则 · 手册 · 性格 · 记忆", "role · rules · manual · persona · memory"), "slate", True),
        ("tools", c.L("工具 schema", "tool schemas"), "slate", True),
        ("user · FULL", c.L("状态板 · 笔记 · 可见历史", "board · notes · history"), "blue", False),
        ("assistant", c.L("思考 · 工具调用 → 工具结果", "thinking · tool calls → results"), "amber", False),
        ("user · DELTA", c.L("新条目 · 状态板变化", "new entries · board changes"), "emerald", False),
        ("assistant", c.L("工具调用 → 工具结果", "tool calls → results"), "amber", False),
        ("user · DELTA", c.L("运行中合并进来的信封", "envelopes merged mid-run"), "emerald", False),
    ]
    note = c.L("前两行创建后逐字节不变，其余只追加 → 供应商的前缀缓存（OpenAI 自动缓存 · DeepSeek · Anthropic `cache_control`）才能命中。暂停 → 继续沿用同一条对话；新局、导入、恢复存档才清空。",
               "The first two rows never change after creation and the rest is only appended → the provider's prefix cache (OpenAI automatic · DeepSeek · Anthropic `cache_control`) can hit. Pause → resume keeps the same conversation; a new game, an import or a restore clears it.")
    cardA = 32 + 14 + len(rows) * 38 + 34 + c.wheight(note, RW - 32, 11.5, 16) + 14
    hdr = c.card(RX, ry, RW, cardA, c.L("对话 · ConversationBook", "Conversation · ConversationBook"), "indigo", sub=c.L("每个 agent 一条", "one per agent"))
    yy = hdr + 14
    for tag, text, theme, lock in rows:
        msg_row(c, RX + 14, yy, RW - 28, tag, text, theme, lock=lock)
        yy += 38
    c.arrow([(RX + RW / 2, yy - 2), (RX + RW / 2, yy + 14)], color="#4f46e5")
    c.text(RX + RW / 2 + 12, yy + 10, c.L("只追加", "append only"), size=11.5, weight=600, fill="#4338ca")
    c.wtext(RX + 16, yy + 36, note, w=RW - 32, size=11.5, fill=INK2, lh=16)

    gy = ry + cardA + 20
    p60 = c.L("60 %：提醒调用 `compact_context`（每条对话一次）→ 重建：丢弃对话，用最新的世界状态 + 笔记新开一条 FULL。重建出来的对话要再长 10 % 窗口才会再次提醒。",
              "60 %: remind the agent once to call `compact_context` → rebuild: drop the conversation and start a fresh FULL one from the current world + notes. A rebuilt conversation is only reminded again after it has grown 10 % of the window.")
    p90 = c.L("90 %：仍未压缩 → 兜底：按完整的轮从最前面裁剪，保留开局信息和最新一轮。",
              "90 %: still not compacted → last resort: trim the oldest complete rounds, keeping the opening and the newest round.")
    cardB = 52 + 62 + c.wheight(p60, RW - 32, 11.5, 16) + 6 + c.wheight(p90, RW - 32, 11.5, 16) + 14
    c.card(RX, gy, RW, cardB, c.L("压缩 · 按需，不设上限", "Compaction · on demand, no cap"), "violet")
    bar_x, bar_w = RX + 20, RW - 40
    bar_y = gy + 56
    c.rect(bar_x, bar_y, bar_w, 16, rx=8, fill="#e2e8f0")
    c.rect(bar_x, bar_y, bar_w * 0.6, 16, rx=8, fill="#34d399")
    c.rect(bar_x + bar_w * 0.6 - 8, bar_y, 8, 16, rx=0, fill="#34d399")
    c.rect(bar_x + bar_w * 0.6, bar_y, bar_w * 0.3, 16, rx=0, fill="#fbbf24")
    c.rect(bar_x + bar_w * 0.9, bar_y, bar_w * 0.1 - 8, 16, rx=0, fill="#fb7185")
    c.rect(bar_x + bar_w - 16, bar_y, 16, 16, rx=8, fill="#fb7185")
    for frac, label, color in ((0.6, "60 % `compact_at`", "#b45309"), (0.9, "90 % `trim_at`", "#be123c")):
        x = bar_x + bar_w * frac
        c.line(x, bar_y - 6, x, bar_y + 22, color=color, sw=2)
        c.text(x, bar_y + 38, label, size=11, weight=700, fill=color, anchor="middle")
    c.text(bar_x, bar_y - 10, c.L("模型的上下文窗口", "the model's context window"), size=11, fill=MUTED)
    yy = bar_y + 62
    yy = c.wtext(RX + 16, yy, p60, w=RW - 32, size=11.5, fill=INK2, lh=16)
    c.wtext(RX + 16, yy + 6, p90, w=RW - 32, size=11.5, fill=INK2, lh=16)

    # ---- what the Brain tells the model (fills the right column down to the loop's bottom)
    by2 = gy + cardB + 20
    full_t = c.L("FULL（对话的第一条）：状态板（现在能不能发言、发言权、待答请求、能力、标签）· 私人笔记与压缩摘要 · 可见历史（有长度预算）· 唤醒原因。",
                 "FULL (first message): the board (can I speak now, floors, open requests, abilities, tags) · private notes and compaction summary · visible history (length-budgeted) · wake reasons.")
    delta_t = c.L("DELTA（之后每次激活或合并）：`seen_seq` 之后的新条目 · 状态板里可行动的变化（`diff_board`）· 唤醒原因；没有新东西就不发。",
                  "DELTA (every later activation or merge): entries after `seen_seq` · actionable changes of the board (`diff_board`) · wake reasons; nothing new, nothing sent.")
    safe_t = c.L("玩家写的内容一律按“不可信数据”处理；称呼统一是 `N3 Carol`（工具参数用 id，说话用名字）。",
                 "What players write is always treated as untrusted data; people are called `N3 Carol` (ids in tool arguments, names in speech).")
    cardC = 32 + 14 + sum(c.wheight(t_, RW - 32, 11.5, 16) + 8 for t_ in (full_t, delta_t, safe_t)) + 6
    c.card(RX, by2, RW, cardC, c.L("Brain 告诉模型什么", "What the Brain tells the model"), "violet")
    yy = by2 + 32 + 20
    for t_ in (full_t, delta_t, safe_t):
        yy = c.wtext(RX + 16, yy, t_, w=RW - 32, size=11.5, fill=INK2, lh=16) + 8

    # ---- bottom: the division of labour
    by = max(loop_bottom, gy + cardB, by2 + cardC) + 28
    half = (X + W + 16 + RW - 28 - 16) / 2
    left = c.L("信箱 `pending` · 一次激活的调度（`call_soon`）· 取消与防饥饿 · 失败重试 · sleep 闹钟 · 停止。同步、从不调用 LLM、从不阻塞。",
               "The `pending` mailbox · scheduling one activation (`call_soon`) · cancellation and the starvation guard · retry on failure · the sleep alarm · stop. Synchronous, never calls the LLM, never blocks.")
    right = c.L("打开对话 → 模型 → 工具 → 合并 → 整理，直到 `sleep`。模型调用返回后检查 `gen`；失败只影响这个 agent，被取消的 Worker 的结果永远不会写进世界。",
                "Open the conversation → model → tools → merge → tidy, until `sleep`. `gen` is checked when the model call returns; a failure only affects this agent, and a cancelled Worker's result is never written into the world.")
    ch = 52 + max(c.wheight(left, half - 32, 12, 17), c.wheight(right, half - 32, 12, 17)) + 14
    c.card(28, by, half, ch, c.L("Actor · 永远在线", "Actor · always on"), "emerald")
    c.wtext(44, by + 52, left, w=half - 32, size=12, fill=INK2, lh=17)
    c.card(28 + half + 16, by, half, ch, c.L("Worker · 一次激活", "Worker · one activation"), "indigo")
    c.wtext(44 + half + 16, by + 52, right, w=half - 32, size=12, fill=INK2, lh=17)
    c.h = by + ch + 24


def build(name: str, lang: str) -> Canvas:
    spec = {
        "overview": (overview, 1040, 960, ("Arena-Agents 整体架构", "Arena-Agents overall architecture"), (
            "浏览器、FastAPI、GameOrchestrator、Runtime（Actor + Worker）、Brain、World 的分层关系，以及 LLM 适配器、工作区与剧本。",
            "Layers: browser, FastAPI, GameOrchestrator, Runtime (Actor + Worker), Brain and World, plus the LLM adapters, the workspace and the scenario.")),
    }
    spec["actor"] = (actor, 1040, 830, ("Actor：永远在线的信箱", "Actor: the always-on mailbox"), (
        "agent.deliver() 的判断树：休眠时 call_soon 安排一次激活；运行时合并，或在 interrupt 命中焦点时取消在途调用；另有闹钟、失败重试与停止三条旁路。",
        "The decision tree of agent.deliver(): schedule one activation when sleeping; merge while running, or cancel the call in flight when an interrupt hits the focus; plus alarm, retry and stop paths."))
    spec["worker"] = (worker, 1040, 1120, ("Worker：一次激活", "Worker: one activation"), (
        "一次激活（一个 asyncio 任务）的循环：打开对话、思考、记账、行动、整理、合并；以及追加式对话与按需压缩。",
        "The loop of one activation (one asyncio task): open the conversation, think, account, act, tidy up, merge; plus the append-only conversation and on-demand compaction."))
    spec["timeline"] = (timeline, 1040, 760, ("时间线：一条消息怎样唤醒所有人", "Timeline: how one message wakes everybody"), (
        "三次提交的时间线：休眠的 agent 被唤醒，正在思考的 agent 在下一轮边界合并，出局的玩家不被广播唤醒。",
        "A timeline of three commits: sleeping agents wake up, an agent that is thinking merges at its next round boundary, a player who is out is not woken by broadcasts."))
    fn, w, h, (tz, te), (dz, de) = spec[name]
    c = Canvas(name, lang, w, h, tz if lang == "zh" else te, dz if lang == "zh" else de)
    fn(c)
    return c


NAMES = ["overview", "actor", "worker", "timeline"]


def render_all(*, force: bool = False, only: list[str] | None = None) -> dict[Path, str]:
    out: dict[Path, str] = {}
    problems: list[str] = []
    for name in only or NAMES:
        for lang in ("zh", "en"):
            c = build(name, lang)
            problems += c.problems
            out[OUT / f"arch-{name}.{lang}.svg"] = c.svg()
    if problems:
        print("layout problems:\n  " + "\n  ".join(problems), file=sys.stderr)
        if not force:
            raise SystemExit(2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="fail when a committed SVG differs from the generated one")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--force", action="store_true", help="write the files even when text would overflow (development)")
    ap.add_argument("--only", nargs="*", help="only these diagrams (names)")
    args = ap.parse_args()
    files = render_all(force=args.force, only=args.only)
    if args.list:
        for p in files:
            print(p.relative_to(ROOT))
        return 0
    if args.check:
        bad = [p for p, s in files.items() if not p.exists() or p.read_text(encoding="utf-8") != s]
        for p in bad:
            print("out of date:", p.relative_to(ROOT), file=sys.stderr)
        return 1 if bad else 0
    OUT.mkdir(parents=True, exist_ok=True)
    for p, s in files.items():
        p.write_text(s, encoding="utf-8", newline="\n")
        print("wrote", p.relative_to(ROOT), f"({len(s) // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
