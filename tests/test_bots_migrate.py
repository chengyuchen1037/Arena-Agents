"""v0.13 roster -> v0.14 bots (H45): names and avatars kept, the persona mapped to L0 by its headings, impressions re-keyed by name,
experiences become low-confidence playbook entries, the record kept; nothing initialized, no model called, nothing of the old
directory touched, idempotent.

Nothing the user wrote is lost (task-35): a hand-written persona without headings - what a persona typed in by hand usually is - is the identity up to
300 characters (cut where a sentence ends) and goes on, word for word, in up to three extra sections ``原有性格（续）`` ...; the voice and the way of thinking
stay empty.  A persona with headings that is longer than a section lets the rest go into the extra sections that are still free before anything is cut.
Only what does not fit in the four places (about 1200 characters) ends with an ellipsis."""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

import pytest

from app.bots.types import CoreFiles
from app.providers.models import ModelBinding
from app.storage import botfiles as bf
from app.storage import bots as bots_mod
from app.storage.bots import BotStore, legacy_bot_id, legacy_persona_to_core
from app.storage.roster import DEFAULT_NAMES, parse_legacy_persona, read_legacy_roster
from app.storage.settings import SettingsStore

SID = "demo"

PERSONA = (
    "【定位】冷静的分析派\n"
    "【说话风格】\n- 话少\n- 爱反问\n"
    "【思考方式】\n- 先看票型\n- 再看发言\n"
    "【决策偏好】\n- 宁可慢一点\n"
    "【压力下】\n- 沉默\n"
    "【口头禅】我再想想"
)
MEMORY = (
    "## 战绩\n场次 5，胜 3，负 2，胜率 60%\n- 角色甲：3 场，胜 2\n- 角色乙：2 场，胜 1\n\n"
    "## 印象\n- N1 Alice：（暂无）\n- N2 Bob：发言短，站边快。\n- N3 Carol：爱带节奏，可信度中。\n\n"
    "## 经验\n- 第一天先听完发言再表态。\n- 少打断别人。\n"
)


def write_roster(
    directory: Path,
    *,
    seats: int = 3,
    names: dict[int, str | None] | None = None,
    personas: dict[int, str] | None = None,
    memories: dict[int, str] | None = None,
    memory_enabled: bool = True,
    avatars: bool = True,
) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    pics = bots_mod.pick_avatars(seats + 1, []) if avatars else [None] * (seats + 1)
    roster: dict = {"memory_enabled": memory_enabled, "seats": {}, "moderator": {"avatar": pics[-1]} if pics[-1] else {}}
    for k in range(1, seats + 1):
        row: dict = {}
        if pics[k - 1]:
            row["avatar"] = pics[k - 1]
        name = (names or {}).get(k, DEFAULT_NAMES[k - 1])
        if name is not None:
            row["name"] = name
        roster["seats"][f"N{k}"] = row
    (directory / "roster.json").write_text(json.dumps(roster, ensure_ascii=False), encoding="utf-8")
    for k, text in (personas or {}).items():
        (directory / f"N{k}.persona.md").write_text(text, encoding="utf-8")
    for k, text in (memories or {}).items():
        (directory / f"N{k}.memory.md").write_text(text, encoding="utf-8")
    return roster


def snapshot(directory: Path) -> dict[str, bytes]:
    return {p.relative_to(directory).as_posix(): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


def new_store(tmp_path: Path, seats: int = 3, **kwargs) -> BotStore:
    return BotStore(tmp_path / "data", seats=seats, library_dir=kwargs.pop("library_dir", tmp_path / "no-library"), **kwargs)


def by_name(store: BotStore) -> dict[str, str]:
    return {name: bot_id for bot_id, name in store.names().items()}


def raw_meta(store: BotStore, bot_id: str) -> dict:
    return json.loads((store.bot_dir(bot_id) / "bot.json").read_text(encoding="utf-8"))


# ======================================================================================================================
# the mapping of a persona
# ======================================================================================================================
def test_a_persona_is_mapped_to_l0_by_its_headings():
    core = legacy_persona_to_core(PERSONA)
    assert core == CoreFiles(
        identity="冷静的分析派",
        voice="话少\n爱反问\n口头禅：我再想想",
        mind="先看票型\n再看发言\n决策偏好：宁可慢一点\n压力下：沉默",
    )
    assert bf.validate_core(core, require_complete=True) == []


@pytest.mark.parametrize(
    "persona,expected",
    [
        ("", CoreFiles()),
        ("   \n\n  ", CoreFiles()),
        ("【定位】只有一句话", CoreFiles(identity="只有一句话")),
        ("【说话风格】\n- 话少", CoreFiles(voice="话少")),  # (no identity: L0 stays incomplete)
        ("一个手写的、没有标题的性格。\n第二行。", CoreFiles(identity="一个手写的、没有标题的性格。\n第二行。")),
        ("前面有一句话\n【定位】后面才是定位\n【说话风格】爱笑", CoreFiles(identity="后面才是定位\n前面有一句话", voice="爱笑")),
        ("定位：冒号写法也认\n说话风格：\n- 短句", CoreFiles(identity="冒号写法也认", voice="短句")),
        ("\ufeff【定位】带 BOM\r\n【说话风格】\r\n- 带 CRLF\r\n", CoreFiles(identity="带 BOM", voice="带 CRLF")),
        ("【定位】**加粗**的\n【口头禅】`代码`", CoreFiles(identity="加粗的", voice="口头禅：代码")),
        ("# 一个标题\n正文\n## 另一个", CoreFiles(identity="一个标题\n正文\n另一个")),  # (no line can split a section of the merged file)
    ],
)
def test_odd_personas_are_read_without_loss_or_error(persona, expected):
    core = legacy_persona_to_core(persona)
    assert core == expected
    assert bf.validate_core(core) == []


def test_a_section_that_is_too_long_goes_on_in_an_extra_section_and_is_not_refused():
    """One endless sentence per section: each is cut at its limit (the cut ends with an ellipsis) and goes on, from the very next character, in the extra
    section of its own that is still free."""
    core = legacy_persona_to_core("【定位】" + "身" * 400 + "\n【说话风格】\n- " + "说" * 700 + "\n【思考方式】\n- " + "想" * 500)
    assert len(core.identity) == 300 and core.identity.endswith("…") and core.identity[:-1] == "身" * 299
    assert len(core.voice) == 600 and core.voice.endswith("…") and len(core.mind) == 400 and core.mind.endswith("…")
    assert core.extras == (("原有·身份（续）", "身" * 101), ("原有·说话方式（续）", "说" * 101), ("原有·分析方式（续）", "想" * 101))
    assert bf.validate_core(core) == []


def test_the_persona_reader_keeps_what_comes_before_the_first_heading():
    assert parse_legacy_persona("开头\n【定位】甲\n- 乙\n【压力下】\n1. 丙\n* 丁") == {"": ["开头"], "定位": ["甲", "乙"], "压力下": ["丙", "丁"]}
    assert parse_legacy_persona("") == {} and parse_legacy_persona(None) == {}  # type: ignore[arg-type]


# ======================================================================================================================
# a long persona: not a word of what the user wrote is lost (task-35)
# ======================================================================================================================
SENTENCES = [
    "他很少先开口，总是先听完所有人的发言。",
    "遇到逻辑不通的地方，他会毫不客气地指出来。",
    "他不喜欢绕弯子，也不喜欢被人牵着走！",
    "你觉得这个人到底站哪边？",
    "他常说：「我只看票型，不看嘴。」",
    "这是他的原则；也是他的习惯。",
]
#: What the hand-written personas of a real v0.13 data directory look like: two or three paragraphs of connected sentences, no heading anywhere, quotes
#: and colons in them, 800 - 1250 characters.
REAL_SHAPE = (
    "你是个玩世不恭的老油条，什么场面都见过，什么人都打过交道，所以很难被谁唬住。你说话爱带三分笑，开口先调侃，再往正题上拐，听上去漫不经心，其实每个字"
    "都在掂量。你最看不惯的是装腔作势的人：越是一本正经地讲道理，你越想戳一戳，看看里面是不是空的。你从不跟人红脸，但也从不替谁圆场，场面冷了，你反而觉得有意思。\n\n"
    "你的习惯很固定：别人发言的时候你不插嘴，只是低头记；轮到你，先说一句不痛不痒的闲话，再说真正想说的。你常说：「别急，慢慢来，反正天又塌不下来。」可这只是你的外壳，"
    "一旦发现有人在带节奏，你的语气会立刻变冷，一句一顿，像在念判决：「你刚才那句话，前后对不上。」\n"
    "你对输赢的态度是：赢了不炫耀，输了不找借口。输了你会笑着说：「这回是我看走眼了。」然后认真复盘自己哪一步想歪了。你相信人是会变的，所以从不给任何人下死结论；"
    "你只记录：他说了什么，做了什么，两者对不对得上。你不迷信直觉，也不迷信逻辑，你迷信的是对照。\n\n"
    "你和人相处的方式很简单：对你好的人，你把话说得客气一点；对你不好的人，你把话说得更客气一点，客气到对方自己觉得不舒服。你不记仇，但记账，谁在什么时候说过什么，你心里都有个数。\n"
    "你看不起两种人：一种是明明没想清楚却抢着表态的，一种是想清楚了却不肯说的。前一种让你烦，后一种让你累。你自己的原则是：想不清楚就先闭嘴，想清楚了就说完整，不绕弯，也不留尾巴。\n\n"
    "你偶尔也会犯糊涂，尤其是在被人夸的时候：别人一夸你聪明，你就容易飘，然后做出过于自信的判断。你知道自己有这个毛病，所以每次被夸，你都会先在心里数三下，再决定要不要接这个话。"
    "你的节奏是慢的：别人急着说，你偏要等一等。你觉得语速和可信度没有关系，但沉默和可信度有关系，一个人在不该沉默的时候沉默，比他说的任何话都更值得注意。\n"
    "你对「规矩」的看法也很有意思：你不反对规矩，只反对拿规矩当挡箭牌的人。规矩是用来让大家都知道边界在哪的，不是用来让某个人心安理得地不动脑子的。"
    "所以你守规矩，但你会盯着那些把规矩挂在嘴边的人，看他们到底是在守，还是在躲。"
)


def free_persona(chars: int, *, paragraph_every: int = 9) -> str:
    """A hand-written persona of exactly ``chars`` characters: sentences one after the other, a blank line after every ``paragraph_every``-th one (0: none)."""
    out, number = "", 0
    while len(out) < chars:
        out += SENTENCES[number % len(SENTENCES)] + ("\n\n" if paragraph_every and number % paragraph_every == paragraph_every - 1 else "")
        number += 1
    return out[:chars]


def ws(text: str) -> str:
    """``text`` without any white space: what has to survive a migration word for word."""
    return re.sub(r"\s+", "", text)


def pieces_of(core: CoreFiles) -> list[str]:
    """What a persona without headings became, in the order it is read: the identity, then the extra sections."""
    return [piece for piece in (core.identity, *(text for _title, text in core.extras)) if piece]


def assert_pieces_are_the_text(original: str, pieces: list[str], *, lost: bool) -> None:
    """The pieces are, one after the other, pieces of ``original`` exactly as it was written: between two of them there is only white space (a stored text is
    stripped; the white space inside a piece is what the user wrote), and a piece that ends with an ellipsis was cut in the middle of a sentence - the text goes
    on in the next one, from the very next character.  ``lost``: the end of the text did not fit and is gone, the last piece says so with its ellipsis;
    otherwise not a character is missing."""
    assert "…" not in original and pieces
    text = original.replace("\r\n", "\n").replace("\r", "\n").strip()
    pos = 0
    for piece in pieces:
        assert piece and piece == piece.strip()
        body = piece[:-1] if piece.endswith("…") else piece
        at = text.find(body, pos)
        assert at >= pos and not text[pos:at].strip(), f"a piece does not follow the one before it: {body[:20]!r}"
        pos = at + len(body)
    if lost:
        assert pieces[-1].endswith("…") and text[pos:].strip(), "the end of the text was not cut"
    else:
        assert not pieces[-1].endswith("…") and not text[pos:].strip(), "something of the text is missing"


def test_a_long_persona_without_headings_keeps_every_word_in_the_identity_and_three_extra_sections():
    text = free_persona(1100)
    core = legacy_persona_to_core(text)
    assert [title for title, _ in core.extras] == ["原有性格（续）", "原有性格（续二）", "原有性格（续三）"]
    assert core.voice == "" and core.mind == ""  # (nothing is made up: the user writes them, or the moderator's model completes them)
    assert 250 < len(core.identity) <= 300 and all(250 < len(body) <= 300 for _, body in core.extras[:2]) and len(core.extras[2][1]) <= 300
    assert all(piece[-1] in "。！？；」" for piece in pieces_of(core)[:-1])  # (cut where a sentence ends)
    assert_pieces_are_the_text(text, pieces_of(core), lost=False)
    assert 0 <= len(text) - sum(len(piece) for piece in pieces_of(core)) <= 6  # (all that is not stored: the white space where two pieces meet)
    assert bf.validate_core(core) == []


def test_a_persona_of_two_thousand_characters_is_cut_at_about_twelve_hundred_with_an_ellipsis():
    text = free_persona(2000)
    core = legacy_persona_to_core(text)
    assert len(core.extras) == 3 and core.voice == "" and core.mind == ""
    assert_pieces_are_the_text(text, pieces_of(core), lost=True)
    total = sum(len(piece) for piece in pieces_of(core))
    assert 1100 <= total <= 1200 and len(core.extras[2][1]) <= 300
    assert all(piece[-1] in "。！？；」" for piece in pieces_of(core)[:-1])  # (the pieces that are not the last end where a sentence ends: only the end is lost)
    assert bf.validate_core(core) == []


@pytest.mark.parametrize(
    "chars,extras,lost",
    [(299, 0, False), (300, 0, False), (301, 1, False), (450, 1, False), (900, 3, False), (1197, 3, False), (1198, 3, True), (3000, 3, True)],
)
def test_what_fits_in_the_four_places_is_never_cut_even_when_it_has_no_stop_at_all(chars: int, extras: int, lost: bool):
    """One endless sentence: every piece but the last is cut in the middle of it (an ellipsis, which takes one of the 300 characters), and what is left goes on
    in the next piece - so 299 + 299 + 299 + 300 = 1197 characters are kept, and the 1198th is the first that is lost."""
    text = "乙" * chars
    core = legacy_persona_to_core(text)
    assert len(core.extras) == extras and core.voice == "" and core.mind == ""
    assert_pieces_are_the_text(text, pieces_of(core), lost=lost)
    assert bf.validate_core(core) == []


def test_a_piece_ends_where_a_sentence_ends_and_only_a_sentence_that_is_too_long_is_cut_in_the_middle():
    short = "他话很少，但每一句都说在点子上。"  # (16 characters)
    long_sentence = "他" + "很" * 448 + "。"  # (450 characters: longer than any of the places)
    text = short * 17 + long_sentence + short * 3
    core = legacy_persona_to_core(text)
    assert core.identity == short * 17  # (272 characters: the next sentence end is far out of reach, so the identity stops at this one - not in the middle of the next)
    assert core.extras[0][1].startswith("他很很") and core.extras[0][1].endswith("很…") and len(core.extras[0][1]) == 300  # (the sentence that is too long: cut at the limit)
    assert core.extras[1][1].startswith("很很")  # (and goes on from the very next character)
    assert_pieces_are_the_text(text, pieces_of(core), lost=False)
    assert bf.validate_core(core) == []


def test_the_places_where_a_piece_may_end():
    from app.storage.bots import _cut_points

    text = "他说：「好。」然后走了！下一句？再来；共3.5倍。\n最后一行"
    cuts = [text[:point] for point in _cut_points(text)]
    assert cuts == [
        "他说：「好。」",  # (the quote that closes the sentence goes with it)
        "他说：「好。」然后走了！",
        "他说：「好。」然后走了！下一句？",
        "他说：「好。」然后走了！下一句？再来；",
        "他说：「好。」然后走了！下一句？再来；共3.5倍。",  # (not after the "." of 3.5; and the line break right after a stop is not a place of its own)
    ]
    assert _cut_points("甲\n乙。\n\n丙") == [1, 4, 5]  # (before a line break: a paragraph, a line of its own)
    assert _cut_points("") == [] and _cut_points("没有句号") == []


def test_white_space_inside_a_piece_is_what_the_user_wrote():
    text = free_persona(1100)
    core = legacy_persona_to_core(text)
    assert "\n\n" in "".join(pieces_of(core)) and text.count("\n\n") >= 1  # (the blank lines between paragraphs are kept)
    # CRLF, a byte-order mark and a tab: read like the same text with plain line ends
    assert legacy_persona_to_core("\ufeff" + text.replace("\n", "\r\n")) == core
    tabbed = "第一行\t带制表符。\n    缩进的第二行。"
    assert legacy_persona_to_core(tabbed) == CoreFiles(identity=tabbed)


def test_a_line_that_would_be_read_as_a_section_heading_loses_its_marks_everywhere_it_lands():
    """The merged file reads ``# ...`` as a heading: such a line is refused by ``validate_core``, so it is neutral in every piece - also one that only starts
    a piece because of where the text was cut."""
    text = "甲。" * 150 + "# 乙乙。" + "丙。" * 100  # (the "# " is in the middle of a line - until the text is cut right in front of it)
    core = legacy_persona_to_core(text)
    assert core.identity == "甲。" * 150 and core.extras[0][1].startswith("乙乙。丙。")  # (the words stay)
    assert bf.validate_core(core) == [] and all(not line.lstrip().startswith("#") for piece in pieces_of(core) for line in piece.split("\n"))
    assert ws(text).replace("#", "") == ws("".join(pieces_of(core)))


def test_a_headed_persona_that_is_too_long_goes_on_in_the_extra_sections_before_anything_is_cut():
    identity = "一个很长的定位。" * 50  # (400 characters)
    voice = [f"他说话很短，从不重复自己的话（{n}）。" for n in range(40)]  # (one line each; about 790 characters together: more than 600, less than 600 + 300)
    mind = [f"他先看事实再听说法，不被气势带着走（{n}）。" for n in range(25)]  # (about 570: more than 400, less than 400 + 300)
    persona = "【定位】" + identity + "\n【说话风格】\n" + "\n".join(f"- {line}" for line in voice) + "\n【思考方式】\n" + "\n".join(f"- {line}" for line in mind)
    core = legacy_persona_to_core(persona)
    extras = dict(core.extras)
    assert list(extras) == ["原有·身份（续）", "原有·说话方式（续）", "原有·分析方式（续）"]  # (one extra section for each section that overflowed, in this order)
    assert len(core.identity) <= 300 and len(core.voice) <= 600 and len(core.mind) <= 400 and all(len(body) <= 300 for body in extras.values())
    assert core.identity.endswith("。") and core.voice.endswith("。") and core.mind.endswith("。")  # (cut where a sentence ends)
    assert ws(core.identity + extras["原有·身份（续）"]) == ws(identity)
    assert ws(core.voice + extras["原有·说话方式（续）"]) == ws("".join(voice))
    assert ws(core.mind + extras["原有·分析方式（续）"]) == ws("".join(mind))
    assert core.complete and bf.validate_core(core, require_complete=True) == []
    # a section that fits does not borrow an extra section
    assert legacy_persona_to_core("【定位】短\n【说话风格】- 话少\n【思考方式】- 先看事实").extras == ()


def test_when_the_extra_sections_are_used_up_the_next_overflow_is_cut_with_an_ellipsis():
    identity = free_persona(1500, paragraph_every=0)  # (needs all the three extra sections, and is cut at its end)
    persona = "【定位】" + identity + "\n【说话风格】\n- " + "说" * 700 + "\n【思考方式】\n- 先看事实"
    core = legacy_persona_to_core(persona)
    assert [title for title, _ in core.extras] == ["原有·身份（续）", "原有·身份（续二）", "原有·身份（续三）"]
    assert core.extras[-1][1].endswith("…")  # (the identity does not fit even in four places)
    assert len(core.voice) == 600 and core.voice.endswith("…")  # (and the voice finds no extra section free: cut at its own limit)
    assert core.mind == "先看事实" and bf.validate_core(core) == []


def test_no_piece_of_any_persona_has_a_line_that_the_merged_file_would_read_as_a_heading():
    """Marks of a heading at the start of a line - several of them, indented, behind a tab - wherever the text is cut: the words stay, the marks go."""
    rnd = random.Random(7)
    atoms = ["他很少先开口。", "# 标题", "# # 双层标题", "  ## 缩进的", "#", "\t# 制表符的", "\u3000# 全角空格的", "不是标题的 # 号", "总是先听完发言；", "说得很短！"]
    for _ in range(300):
        text = "".join(rnd.choice(atoms) + rnd.choice(["", "", "\n", " ", "\n\n"]) for _ in range(rnd.randint(1, 140)))
        if not text.strip():
            continue
        for persona in (text, "【定位】" + text.replace("\n", " ") + "\n【说话风格】\n- " + text.replace("\n", "\n- ")):
            core = legacy_persona_to_core(persona)
            assert bf.validate_core(core) == [], persona[:60]
            joined = "".join(pieces_of(core)) + core.voice + core.mind
            if not joined.rstrip().endswith("…") and len(persona.strip()) <= 1000 and persona is text:
                assert ws(text).replace("#", "") == ws(joined).replace("#", ""), text[:60]  # (nothing but the marks is gone)


def test_the_titles_of_the_extra_sections_are_read_back_in_the_order_they_were_written():
    """The files of the extra sections are read in the order of their slugs, and the slug of a title is made of its ASCII characters: a title like
    "续 2" would come back before "续" and the text of a persona would be read in the wrong order.  These titles have no ASCII character at all."""
    core = legacy_persona_to_core(free_persona(1100))
    titles = [title for title, _ in core.extras]
    slugs = bf.assign_slugs(titles)
    assert slugs == sorted(slugs) and len(set(slugs)) == 3
    assert all(len(title) <= bf.EXTRA_TITLE_CHARS and title.isascii() is False and not re.search(r"[A-Za-z0-9]", title) for title in titles)
    headed = legacy_persona_to_core("【定位】" + "身" * 1500 + "\n【说话风格】\n- " + "说" * 700)
    assert bf.assign_slugs([title for title, _ in headed.extras]) == sorted(bf.assign_slugs([title for title, _ in headed.extras]))


def test_the_pieces_of_random_personas_are_always_the_text_in_order():
    """Many hand-written texts of every length - stops, quotes, line breaks, long sentences, no stops at all: whatever comes out passes ``validate_core``,
    keeps every word in order, and loses something only when it did not fit (then the last piece says so)."""
    rnd = random.Random(20251120)
    atoms = ["他很少先开口", "总是先听完所有人的发言", "遇到逻辑不通的地方", "会毫不客气地指出来", "不喜欢绕弯子", "我只看票型，不看嘴", "who knows", "共3.5倍", "他说：「好」"]
    ends = ["。", "！", "？", "；", "。」", "", "\n", "\n\n", "。\n"]
    for _ in range(400):
        sentences = rnd.randint(1, 110)
        text = "".join(
            rnd.choice(atoms) + (rnd.choice("，、") + rnd.choice(atoms) if rnd.random() < 0.5 else "") + rnd.choice(ends) for _ in range(sentences)
        )
        if rnd.random() < 0.15:
            text = text.replace("。", "")  # (hardly any stop)
        stripped = text.strip()
        if not stripped:
            continue
        core = legacy_persona_to_core(text)
        pieces = pieces_of(core)
        assert bf.validate_core(core) == [] and core.voice == "" and core.mind == "" and len(core.extras) <= 3
        lost = pieces[-1].endswith("…")
        assert not (lost and len(stripped) <= 1197), "a text that fits was cut"
        assert_pieces_are_the_text(text, pieces, lost=lost)
        if lost:
            assert sum(len(piece) for piece in pieces) >= 1100  # (nothing like four sentences' worth of room is wasted: the sentences are short)


# ======================================================================================================================
# the whole roster
# ======================================================================================================================
def test_a_roster_becomes_bots_and_the_old_directory_is_left_alone(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    roster = write_roster(
        roster_dir,
        names={2: "Bobby"},
        personas={1: PERSONA},
        memories={1: MEMORY.replace("- N3 Carol", "- N3 Carol"), 2: "## 战绩\n场次 1，胜 0，负 1，胜率 0%\n- 角色甲：1 场，胜 0\n"},
        memory_enabled=False,
    )
    before = snapshot(roster_dir)
    store = new_store(tmp_path)
    ids = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert snapshot(roster_dir) == before  # (read only)
    assert store.lineup() == ids and len(ids) == 3
    assert [store.name_of(i) for i in ids] == ["Alice", "Bobby", "Carol"]
    assert [raw_meta(store, i)["avatar"] for i in ids] == [roster["seats"][f"N{k}"]["avatar"] for k in (1, 2, 3)]
    assert store.moderator()["avatar"] == roster["moderator"]["avatar"] and store.memory_enabled() is False
    alice, bobby, carol = ids
    assert all(raw_meta(store, i)["origin"] == "migrated" and raw_meta(store, i)["init"]["status"] == "uninitialized" for i in ids)
    assert store.core(alice) == legacy_persona_to_core(PERSONA) and store.core(alice).complete
    assert store.core(bobby) == CoreFiles() and store.core(carol) == CoreFiles()  # (an empty persona: L0 is missing)
    # the impressions are kept by the NAME of the seat they were about (as the roster named it)
    assert bf.parse_people(store.read_part(alice, "people")) == {"Bobby": "发言短，站边快。", "Carol": "爱带节奏，可信度中。"}
    assert store.read_part(bobby, "people") == ""
    # the experiences became playbook entries with the lowest confidence, filed under the scenario
    entries = bf.parse_playbook(store.read_part(alice, "playbook", SID))
    assert [(e.text, e.confidence, e.support, e.counter, e.source, e.tags) for e in entries] == [
        ("第一天先听完发言再表态。", "低", 0, 0, "迁移自 v0.13 的经验", ()),
        ("少打断别人。", "低", 0, 0, "迁移自 v0.13 的经验", ()),
    ]
    assert store.read_part(bobby, "playbook", SID) == ""
    # the record is kept
    assert bf.read_record(store.bot_dir(alice)).by_role == {"角色甲": [3, 2], "角色乙": [2, 1]} and bf.read_record(store.bot_dir(alice)).games == 5
    assert store.load_mind(alice, SID).record.startswith("场次 5，胜 3，负 2，胜率 60%")
    assert store.load_mind(bobby, SID).record.startswith("场次 1，胜 0")
    assert store.load_mind(carol, SID).record == ""
    # nothing was initialized: no notes, no digest; and the ids are the ones the migration makes
    assert store.read_part(alice, "arch_notes") == "" and store.read_part(alice, "digest", SID) == ""
    assert ids == [legacy_bot_id(1, "Alice"), legacy_bot_id(2, "Bobby"), legacy_bot_id(3, "Carol")]
    # a bot that has experiences has learned something: it is frozen (renaming it needs a confirmation)
    assert store.is_frozen(alice) and not store.is_frozen(bobby)
    assert store.check_integrity() == []


def test_impressions_follow_the_names_not_the_seats(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    memories = {
        1: "## 印象\n- N2 Bob：话多。\n- N3 Carol：话少。\n- N1 自己：不会被收下\n- N9 Nobody：没有这个座位\n",
        2: "## 印象\n- N1 Alice：（暂无）\n- N3 Carol:半角冒号也行。\n",
        3: "## 印象\n- N1 Alice：爱带节奏。\n- N2 Bob： \n",
    }
    write_roster(roster_dir, names={1: "Ann", 2: "Ben", 3: "Cat"}, memories=memories)
    store = new_store(tmp_path)
    ann, ben, cat = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert bf.parse_people(store.read_part(ann, "people")) == {"Ben": "话多。", "Cat": "话少。"}
    assert bf.parse_people(store.read_part(ben, "people")) == {"Cat": "半角冒号也行。"}
    assert bf.parse_people(store.read_part(cat, "people")) == {"Ann": "爱带节奏。"}
    # (what is mounted in a game follows the names)
    assert store.load_mind(ann, SID, others=["Ben", "Cat"]).people == {"Ben": "话多。", "Cat": "话少。"}


def test_a_v0_13_0_memory_file_is_read_and_its_old_text_is_not_migrated(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    old = "## 战绩\n场次 2，胜 1，负 1，胜率 50%\n\n## 对局记忆\n第一局：太保守。\n\n## 经验与印象\nBob 爱撒谎。\n"
    write_roster(roster_dir, memories={1: old})
    (roster_dir / "N1.memory.md").write_bytes(b"\xef\xbb\xbf" + old.replace("\n", "\r\n").encode("utf-8"))  # (a BOM and CRLF too)
    store = new_store(tmp_path)
    alice = store.import_legacy_roster(roster_dir, scenario_id=SID)[0]
    assert bf.read_record(store.bot_dir(alice)).games == 2 and bf.read_record(store.bot_dir(alice)).wins == 1  # the record survives
    assert store.read_part(alice, "people") == "" and store.read_part(alice, "playbook", SID) == ""
    assert not any("太保守" in p.read_text(encoding="utf-8", errors="ignore") for p in store.root.rglob("*") if p.is_file())
    assert (roster_dir / "N1.memory.md").read_bytes().startswith(b"\xef\xbb\xbf")  # (and the old file is as it was)


def test_experiences_are_cut_to_an_entry_and_need_a_scenario_to_live_in(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    long_lesson = "很长的一条经验" * 20
    write_roster(roster_dir, memories={1: f"## 经验\n- {long_lesson}\n- 第二条\n- 第三条\n"})
    store = new_store(tmp_path)
    alice = store.import_legacy_roster(roster_dir, scenario_id=SID)[0]
    entries = bf.parse_playbook(store.read_part(alice, "playbook", SID))
    assert len(entries) == 3 and len(entries[0].text) == 80 and entries[0].text.endswith("…") and bf.check_playbook(entries) == []
    other = new_store(tmp_path / "other")
    alice2 = other.import_legacy_roster(roster_dir, scenario_id="")[0]  # (no scenario: nowhere to put them)
    assert not list((other.bot_dir(alice2) / "rules").glob("*")) if (other.bot_dir(alice2) / "rules").exists() else True
    assert not other.is_frozen(alice2)
    with pytest.raises(bf.BotFileError):
        new_store(tmp_path / "third").import_legacy_roster(roster_dir, scenario_id="../escape")


def test_the_default_scenario_of_the_program_is_used_when_none_is_given(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, memories={1: "## 经验\n- 一条经验\n"})
    assert bots_mod.default_scenario_id() == "werewolf_12p"  # (config.yml: scenarios/werewolf_12p.md, its front matter id)
    store = new_store(tmp_path)
    alice = store.import_legacy_roster(roster_dir)[0]
    assert (store.bot_dir(alice) / "rules" / "werewolf_12p" / "playbook.md").is_file()
    assert [e.text for e in bf.parse_playbook(store.read_part(alice, "playbook", "werewolf_12p"))] == ["一条经验"]


# ======================================================================================================================
# odd rosters
# ======================================================================================================================
def test_names_that_are_not_valid_any_more_are_made_valid_and_unique(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, seats=5, names={1: "Re:Mix", 2: "re mix", 3: "a" * 30, 4: None, 5: "bob"}, memories={5: "## 印象\n- N1 Re:Mix：旧名字里带冒号。\n"})
    store = new_store(tmp_path, seats=5)
    ids = store.import_legacy_roster(roster_dir, scenario_id=SID)
    names = [store.name_of(i) for i in ids]
    assert names == ["Re Mix", "re mix2", "Carol", "David", "bob"]  # (colon -> space; the same name twice: numbered; too long or none: the default of the seat, as v0.13 read it)
    assert len({n.casefold() for n in names}) == 5 and all(store.validate_name(n, exclude=i) == n for n, i in zip(names, ids))
    assert bf.parse_people(store.read_part(ids[4], "people")) == {"Re Mix": "旧名字里带冒号。"}  # (the key is the name as it is now)
    assert store.load_mind(ids[4], SID, others=["Re Mix"]).people == {"Re Mix": "旧名字里带冒号。"}


def test_a_missing_or_broken_roster_still_gives_a_table(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    store = new_store(tmp_path)
    ids = store.import_legacy_roster(empty, scenario_id=SID)
    assert [store.name_of(i) for i in ids] == ["Alice", "Bob", "Carol"] and all(not store.core(i).complete for i in ids)
    assert all(raw_meta(store, i)["avatar"] for i in ids) and store.moderator()["avatar"]  # (no avatars in the roster: new ones)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "roster.json").write_text("not json", encoding="utf-8")
    (broken / "N2.persona.md").write_bytes(b"\xff\xfe\x00bad bytes")
    other = new_store(tmp_path / "x")
    assert len(other.import_legacy_roster(broken, scenario_id=SID)) == 3
    nowhere = new_store(tmp_path / "y")
    assert len(nowhere.import_legacy_roster(tmp_path / "does-not-exist", scenario_id=SID)) == 3
    assert read_legacy_roster(tmp_path / "does-not-exist", 2).seats[1].name == "Bob"


def test_the_seats_of_the_old_roster_beyond_the_table_are_ignored(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, seats=12, personas={7: PERSONA, 2: PERSONA}, memories={2: "## 印象\n- N7 Grace：爱带节奏。\n- N3 Carol：话少。\n"})
    store = new_store(tmp_path, seats=3)
    alice, bob, carol = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert store.core(bob).complete and bf.parse_people(store.read_part(bob, "people")) == {"Carol": "话少。"}  # (Grace is not at this table)


def test_an_avatar_that_is_not_in_the_roster_gets_a_new_one_distinct_from_the_others(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    roster = write_roster(roster_dir, seats=3)
    del roster["seats"]["N2"]["avatar"]
    (roster_dir / "roster.json").write_text(json.dumps(roster, ensure_ascii=False), encoding="utf-8")
    store = new_store(tmp_path)
    ids = store.import_legacy_roster(roster_dir, scenario_id=SID)
    avatars = [raw_meta(store, i)["avatar"] for i in ids]
    assert avatars[0] == roster["seats"]["N1"]["avatar"] and avatars[2] == roster["seats"]["N3"]["avatar"]
    assert avatars[1] and avatars[1] not in (avatars[0], avatars[2], store.moderator()["avatar"])


# ======================================================================================================================
# idempotent, replacing, the bindings
# ======================================================================================================================
def test_migrating_the_same_roster_twice_gives_the_same_bots(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, personas={1: PERSONA}, memories={1: MEMORY})
    store = new_store(tmp_path, clock=lambda: 1_763_600_000.0)
    first = store.import_legacy_roster(roster_dir, scenario_id=SID)
    state = snapshot(store.root)
    second = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert second == first and snapshot(store.root) == state  # (byte for byte)
    assert sorted(p.name for p in store.root.iterdir()) == sorted([*first, "lineup.json"])
    # a table that exists is left alone when asked not to replace it
    store.write_part(first[0], "digest", "我自己写的要点", SID)
    assert store.import_legacy_roster(roster_dir, scenario_id=SID, replace=False) == first
    assert store.read_part(first[0], "digest", SID) == "我自己写的要点"
    store.import_legacy_roster(roster_dir, scenario_id=SID)  # (replace: what was written since is gone)
    assert store.read_part(first[0], "digest", SID) == ""


def test_the_bots_that_were_there_are_replaced_with_their_bindings(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, names={1: "Ann"})
    store = new_store(tmp_path, library_dir=tmp_path / "lib")
    store.bootstrap()
    old = store.lineup()
    settings = SettingsStore(store.data_dir)
    low = ModelBinding(provider_id="p", model_id="m1", reasoning_depth="low")
    high = ModelBinding(provider_id="p", model_id="m2", reasoning_depth="high")
    settings.save_bot_binding(old[0], low)  # (a binding of a bot that is about to be replaced)
    # what v0.13 saved by seat sits in settings.json next to it, as in a data directory that is being restored from an old archive
    payload = json.loads((store.data_dir / "settings.json").read_text(encoding="utf-8"))
    payload["default_bindings"].update({"N2": high.model_dump(mode="json"), "moderator": low.model_dump(mode="json")})
    (store.data_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
    new = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert not set(old) & set(new) and sorted(p.name for p in store.root.iterdir()) == sorted([*new, "lineup.json"])  # the old directories are gone
    stored = json.loads((store.data_dir / "settings.json").read_text(encoding="utf-8"))["default_bindings"]
    assert set(stored) == {new[1], "moderator"} and settings.get_bot_binding(new[1]) == high  # (seat 2 -> the bot in seat 2; the dead bot's is forgotten)
    assert settings.get_default_bindings()["N2"] == high


def test_a_bot_without_a_persona_gets_the_default_core_of_its_name(tmp_path: Path):
    """H59: v0.13 personas were empty by default; a bot that keeps its default name gets the default library's L0 for it."""
    lib = tmp_path / "lib"
    for name in ("Alice", "Bob"):
        (lib / name / "core").mkdir(parents=True)
        (lib / name / "bot.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        for key in ("identity", "voice", "mind"):
            (lib / name / "core" / f"{key}.md").write_text(f"{name} 的 {key}\n", encoding="utf-8")
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, names={2: "Bobby"}, personas={1: PERSONA}, memories={3: MEMORY})
    store = new_store(tmp_path / "a", library_dir=lib)
    alice, bobby, carol = store.import_legacy_roster(roster_dir, scenario_id=SID)  # (the default: fill)
    assert store.core(alice) == legacy_persona_to_core(PERSONA)  # (a persona that was written is never replaced)
    assert store.core(bobby) == CoreFiles() and store.core(carol) == CoreFiles()  # (no default is named Bobby or Carol here)
    # a roster whose seats keep the default names
    write_roster(tmp_path / "roster2", names={2: "bob"}, memories={2: MEMORY.replace("N3 Carol", "N3 Carol")})
    other = new_store(tmp_path / "b", library_dir=lib)
    ids = other.import_legacy_roster(tmp_path / "roster2", scenario_id=SID)
    assert other.core(ids[0]).identity == "Alice 的 identity" and other.core(ids[1]).voice == "Bob 的 voice"  # (any case of the name)
    assert other.core(ids[2]) == CoreFiles()
    assert raw_meta(other, ids[1])["origin"] == "migrated" and other.name_of(ids[1]) == "bob"  # (the name stays what the roster had)
    assert bf.read_record(other.bot_dir(ids[1])).games == 5 and other.is_frozen(ids[1])  # (and so does the memory)
    assert other.summary(ids[0])["core"]["complete"] is True
    # the switch: without it the core stays missing, as in H45
    off = new_store(tmp_path / "c", library_dir=lib)
    ids = off.import_legacy_roster(tmp_path / "roster2", scenario_id=SID, fill_missing_core=False)
    assert all(off.core(i) == CoreFiles() for i in ids)


def library_with(tmp_path: Path, *names: str) -> Path:
    """A default library of these names; the L0 of each says ``<name> 的 identity`` / ``voice`` / ``mind`` (so that it can be told from anything else)."""
    lib = tmp_path / "lib"
    for name in names:
        (lib / name / "core").mkdir(parents=True)
        (lib / name / "bot.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        for key in ("identity", "voice", "mind"):
            (lib / name / "core" / f"{key}.md").write_text(f"{name} 的 {key}\n", encoding="utf-8")
    return lib


# ======================================================================================================================
# a long hand-written persona, the whole way through (task-35)
# ======================================================================================================================
def test_a_long_hand_written_persona_comes_through_the_migration_without_losing_a_word(tmp_path: Path):
    assert 800 <= len(REAL_SHAPE) <= 1197 and "\n\n" in REAL_SHAPE and "：" in REAL_SHAPE and "「" in REAL_SHAPE  # (the shape of the real ones)
    too_long = REAL_SHAPE + "\n" + free_persona(400)
    lib = library_with(tmp_path, "Alice", "Bob", "Carol")
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, seats=3, personas={2: REAL_SHAPE, 3: too_long})  # (Bob and Carol: the names of two bots of the library - and a persona of their own)
    before = snapshot(roster_dir)
    store = new_store(tmp_path, library_dir=lib, clock=lambda: 1_763_600_000.0)
    alice, bob, carol = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert snapshot(roster_dir) == before  # (read only: the old directory is as it was)
    assert store.core(alice).identity == "Alice 的 identity" and store.core(alice).complete  # (no persona and a default name: H59, as before)

    core = store.core(bob)  # (read back from the files: the extra sections come back in the order they were written)
    assert core == legacy_persona_to_core(REAL_SHAPE)
    assert core.identity and core.extras and core.voice == "" and core.mind == ""  # (the library's Bob voice is NOT mixed into the words of this Bob)
    assert_pieces_are_the_text(REAL_SHAPE, pieces_of(core), lost=False)
    cut = store.core(carol)
    assert cut == legacy_persona_to_core(too_long) and len(cut.extras) == 3
    assert_pieces_are_the_text(too_long, pieces_of(cut), lost=True)

    # what the start check says: the voice and the way of thinking are missing, and the bot is not initialized
    items = store.problems(bob)
    assert [item["code"] for item in items] == ["core_incomplete", "not_initialized"]
    assert "缺少「说话方式」、「分析方式」" in items[0]["message"] and items[0]["fix"] == "edit_core"
    assert store.summary(bob)["core"]["complete"] is False
    # what the panel shows: the identity, and the carrying-on sections with their text
    detail = store.detail(bob)
    assert [extra["title"] for extra in detail["core"]["extras"]] == [title for title, _ in core.extras]
    assert [extra["text"] for extra in detail["core"]["extras"]] == [text for _, text in core.extras]
    assert detail["core"]["sections"] == {"identity": core.identity, "voice": "", "mind": ""}
    assert all(f"# 附加：{title}" in detail["core"]["markdown"] for title, _ in core.extras)
    assert bf.parse_core_markdown(detail["core"]["markdown"], require_complete=False).core == core  # (the file the user exports and imports again)

    # migrating again changes nothing, byte for byte; neither does doing it in another table
    state = snapshot(store.root)
    assert store.import_legacy_roster(roster_dir, scenario_id=SID) == [alice, bob, carol] and snapshot(store.root) == state
    other = new_store(tmp_path / "other", library_dir=lib, clock=lambda: 1_763_600_000.0)
    other_ids = other.import_legacy_roster(roster_dir, scenario_id=SID)
    assert other_ids == [alice, bob, carol] and [other.core(i) for i in other_ids] == [store.core(i) for i in (alice, bob, carol)]
    assert snapshot(roster_dir) == before


def test_a_headed_persona_that_overflows_survives_the_files_in_order(tmp_path: Path):
    """The persona of the v0.13.1 template whose sections are longer than their new limits: the overflow sections are written, and read back, in the order of
    the sections (身份, 说话方式, 分析方式)."""
    persona = (
        "【定位】" + "一个很长的定位。" * 50 + "\n【说话风格】\n" + "\n".join(f"- 他说话很短，从不重复自己的话（{n}）。" for n in range(40))
        + "\n【思考方式】\n" + "\n".join(f"- 他先看事实再听说法，不被气势带着走（{n}）。" for n in range(25))
    )
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, seats=3, personas={2: persona})
    store = new_store(tmp_path, library_dir=tmp_path / "no-library")
    _alice, bob, _carol = store.import_legacy_roster(roster_dir, scenario_id=SID)
    core = store.core(bob)
    assert core == legacy_persona_to_core(persona) and core.complete and bf.validate_core(core, require_complete=True) == []
    assert [title for title, _ in core.extras] == ["原有·身份（续）", "原有·说话方式（续）", "原有·分析方式（续）"]
    assert store.problems(bob)[0]["code"] == "not_initialized"  # (a complete L0: only the initialization is missing)


def test_the_first_start_keeps_the_hand_written_personas_and_does_not_complete_them_from_the_library(isolated_external_storage: Path):
    """The real case: a v0.13 data directory in which five of the twelve seats - with default names - have a long persona of the user's own, and the other
    seven have none."""
    data = isolated_external_storage
    personas = {2: free_persona(800), 3: REAL_SHAPE, 5: free_persona(1100), 8: free_persona(1197), 9: free_persona(1250)}
    write_roster(data / "roster", seats=12, personas=personas)
    before = snapshot(data / "roster")
    store = bots_mod.get_bot_store()
    library = {b.name: bf.read_core(b.path / "core") for b in store.library()}
    for seat, persona in personas.items():
        bot_id = store.lineup()[seat - 1]
        name = store.name_of(bot_id)
        core = store.core(bot_id)
        assert name == DEFAULT_NAMES[seat - 1] and name in library  # (a default name: the library has a bot of it, and its words are not used)
        assert core == legacy_persona_to_core(persona) and core.identity and core.voice == "" and core.mind == "" and core.identity != library[name].identity
        assert_pieces_are_the_text(persona, pieces_of(core), lost=len(persona.strip()) > 1197)  # (only the 1250 characters do not fit in the four places)
        assert not core.complete and store.problems(bot_id)[0]["code"] == "core_incomplete"
    for seat in (1, 4, 6, 7, 10, 11, 12):  # (the seven that wrote nothing: the default L0 of their name, as before)
        bot_id = store.lineup()[seat - 1]
        assert store.core(bot_id) == library[store.name_of(bot_id)] and store.core(bot_id).complete
    assert store.check_integrity() == [] and snapshot(data / "roster") == before


def test_a_persona_that_cannot_be_mapped_is_left_out_and_does_not_take_the_table_with_it(tmp_path: Path, monkeypatch, caplog):
    """The mapping keeps to the limits, so this cannot happen - but if it ever does, one old text must not make the whole migration fail (a failed migration
    gives the user twelve default bots instead of their own): that bot has no L0 (and the library's, if its name is a default one), the others are as they were."""
    roster_dir = tmp_path / "roster"
    write_roster(roster_dir, seats=3, personas={1: PERSONA, 2: "会被弄坏的性格。", 3: PERSONA})
    real = bots_mod.legacy_persona_to_core
    monkeypatch.setattr(bots_mod, "legacy_persona_to_core", lambda text: CoreFiles(identity="太长" * 200) if "弄坏" in text else real(text))
    store = new_store(tmp_path)
    with caplog.at_level("WARNING"):
        alice, bob, carol = store.import_legacy_roster(roster_dir, scenario_id=SID)
    assert store.core(alice) == real(PERSONA) and store.core(carol) == real(PERSONA) and store.core(bob) == CoreFiles()
    assert "left out" in caplog.text and store.check_integrity() == []


def test_the_record_of_a_v0_13_0_memory_file_is_read_from_its_record_section_and_from_nowhere_else(tmp_path: Path):
    """The memory files of a v0.13.0 data directory hold ``## 战绩`` and ``## 对局记忆`` (no impressions, no lessons).  The record that is migrated is the one
    in the first: the numbers that stand in the free text of the second - or in a file that has no record section - are nobody's record."""
    roster_dir = tmp_path / "roster"
    seat_1 = "## 战绩\n场次 2，胜 0，负 2，胜率 0%\n- 角色甲：1 场，胜 0\n- 角色乙：1 场，胜 0\n\n## 对局记忆\n第一局：太保守了，场次 9，胜 9。\n第二局：被带了节奏。\n"
    seat_2 = "## 战绩\n场次 7，胜 4，负 3，胜率 57%\n- 角色甲：7 场，胜 4\n"
    seat_3 = "## 对局记忆\n场次 3，胜 3：这一行不是战绩。\n"
    write_roster(roster_dir, seats=4, memories={1: seat_1, 2: seat_2, 3: seat_3})
    before = snapshot(roster_dir)
    store = new_store(tmp_path, seats=4)
    one, two, three, four = store.import_legacy_roster(roster_dir, scenario_id=SID)
    record = bf.read_record(store.bot_dir(one))
    assert (record.games, record.wins, record.by_role) == (2, 0, {"角色甲": [1, 0], "角色乙": [1, 0]})
    assert json.loads((store.bot_dir(one) / "record.json").read_text(encoding="utf-8")) == {"games": 2, "wins": 0, "by_role": {"角色乙": [1, 0], "角色甲": [1, 0]}}
    assert store.load_mind(one, SID).record.startswith("场次 2，胜 0，负 2，胜率 0%")  # (what the bot reads about itself)
    record = bf.read_record(store.bot_dir(two))
    assert (record.games, record.wins, record.by_role) == (7, 4, {"角色甲": [7, 4]})  # (each bot has the record of its own file)
    for empty in (three, four):  # (no record section: no record - the numbers in the text of the old stories are not read)
        assert not (store.bot_dir(empty) / "record.json").exists() and bf.read_record(store.bot_dir(empty)).games == 0
    assert not any("太保守" in path.read_text(encoding="utf-8", errors="ignore") for path in store.root.rglob("*") if path.is_file())  # (the old text is not migrated)
    assert snapshot(roster_dir) == before


# ======================================================================================================================
# the first start of a v0.13 data directory
# ======================================================================================================================
def test_the_first_start_migrates_the_old_roster_of_the_data_directory(isolated_external_storage: Path):
    from app.storage import clear_store_caches

    data = isolated_external_storage
    roster_dir = data / "roster"
    write_roster(roster_dir, seats=12, names={1: "Ann", 12: "Zed"}, personas={1: PERSONA}, memories={1: MEMORY.replace("N2 Bob", "N2 Bob").replace("N3 Carol", "N3 Carol")})
    before = snapshot(roster_dir)
    store = bots_mod.get_bot_store()
    names = list(store.names().values())
    assert len(names) == 12 and names[0] == "Ann" and names[11] == "Zed" and names[1] == "Bob"
    assert store.core(store.lineup()[0]) == legacy_persona_to_core(PERSONA) and raw_meta(store, store.lineup()[0])["origin"] == "migrated"
    library = {b.name: b for b in store.library()}
    assert store.core(store.lineup()[1]) == bf.read_core(library["Bob"].path / "core") and store.core(store.lineup()[1]).complete  # (H59: default names get the default L0)
    assert store.core(store.lineup()[11]) == CoreFiles()  # (a bot that was renamed to something that is no default: no L0)
    assert snapshot(roster_dir) == before  # (the old roster is kept)
    assert bf.parse_people(store.read_part(store.lineup()[0], "people")) == {"Bob": "发言短，站边快。", "Carol": "爱带节奏，可信度中。"}
    # once the bots exist the roster is not read again
    (roster_dir / "roster.json").write_text(json.dumps({"seats": {"N1": {"name": "Changed"}}}), encoding="utf-8")
    clear_store_caches()
    assert bots_mod.get_bot_store().name_of(store.lineup()[0]) == "Ann"
    # a data directory with bots but a lost line-up is rebuilt from the bots, not from the roster
    (data / "bots" / "lineup.json").unlink()
    clear_store_caches()
    assert sorted(bots_mod.get_bot_store().lineup()) == sorted(store.lineup())


def test_a_migration_that_fails_falls_back_to_the_default_library(isolated_external_storage: Path, monkeypatch):
    data = isolated_external_storage
    write_roster(data / "roster", seats=12)

    def boom(self, roster_dir, **kwargs):
        # (it got as far as writing a bot)
        self._create_bot(name="Half", avatar=None, origin="migrated")
        raise RuntimeError("something is wrong with this roster")

    monkeypatch.setattr(BotStore, "import_legacy_roster", boom)
    store = bots_mod.get_bot_store()
    assert len(store.lineup()) == 12 and "Half" not in store.names().values()  # (nothing of the failed attempt stays)
    assert sorted(p.name for p in (data / "bots").iterdir()) == sorted([*store.lineup(), "lineup.json"])


def test_without_an_old_roster_the_first_start_uses_the_default_library(isolated_external_storage: Path):
    store = bots_mod.get_bot_store()
    assert len(store.lineup()) == 12 and [store.name_of(i) for i in store.lineup()] == list(DEFAULT_NAMES)
    assert all(raw_meta(store, i)["origin"] in ("default", "user") for i in store.lineup())


def test_the_old_reader_is_lenient_about_everything(tmp_path: Path):
    roster_dir = tmp_path / "roster"
    roster_dir.mkdir()
    (roster_dir / "roster.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"seats": {"N1": {"name": "Ann", "avatar": {"v": 1}}, "N2": "oops", "N3": {"name": ["x"], "avatar": []}}}).encode("utf-8"))
    (roster_dir / "N1.persona.md").write_bytes(b"\xef\xbb\xbf" + "【定位】带 BOM 的\n".encode("utf-8"))
    (roster_dir / "N1.memory.md").write_text("## 印象\n- N2 Bob：话少\n", encoding="utf-8")
    legacy = read_legacy_roster(roster_dir, 3)
    assert legacy.memory_enabled is True and legacy.moderator_avatar is None
    assert [r.name for r in legacy.seats] == ["Ann", "Bob", "Carol"] and legacy.names == {"N1": "Ann", "N2": "Bob", "N3": "Carol"}
    assert legacy.seats[0].avatar == {"v": 1} and legacy.seats[1].avatar is None and legacy.seats[2].avatar is None
    assert legacy.seats[0].persona == "【定位】带 BOM 的" and legacy.seats[0].parts.impressions["N2"] == "话少"
    assert legacy.seats[1].persona == "" and legacy.seats[1].parts.empty


def test_a_legacy_name_is_made_valid_without_losing_what_can_be_kept():
    from app.storage.bots import _legacy_name

    assert _legacy_name("Re:Mix", 1) == "Re Mix" and _legacy_name("a：b", 1) == "a b" and _legacy_name("  x  y  ", 1) == "x y"
    assert _legacy_name("a" * 30, 3) == "a" * 20
    assert _legacy_name('a"b<c>d\\e', 1) == "abcde"
    assert _legacy_name("::", 4) == "David" and _legacy_name("", 2) == "Bob" and _legacy_name(None, 3) == "Carol"  # type: ignore[arg-type]


# ======================================================================================================================
# the document says what the code does
# ======================================================================================================================
def test_the_bot_document_says_what_the_migration_does_with_a_persona():
    """docs/bots.md (section 9.2) names the limits and the titles of the extra sections: they are the ones of the code."""
    from app.bots.types import CORE_EXTRA_CHARS, CORE_EXTRA_MAX, CORE_LIMITS

    doc = (Path(__file__).resolve().parents[1] / "docs" / "bots.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    free = next(line for line in doc.splitlines() if line.startswith("| **没有标题的手写性格**"))
    headed = next(line for line in doc.splitlines() if line.startswith("| 带标题的性格里，某一段超过了新的字数上限"))
    assert f"前 {CORE_LIMITS['identity']} 字" in free and f"最多 {CORE_EXTRA_MAX} 个附加分区" in free and f"每份 ≤ {CORE_EXTRA_CHARS} 字" in free
    assert f"身份 {CORE_LIMITS['identity']} 字 + {CORE_EXTRA_MAX} × {CORE_EXTRA_CHARS} 字" in doc
    for title, _ in legacy_persona_to_core(free_persona(1100)).extras:
        assert f"“{title}”" in free, title
    overflow = legacy_persona_to_core("【定位】" + "身" * 400 + "\n【说话风格】\n- " + "说" * 700 + "\n【思考方式】\n- " + "想" * 500)
    for title, _ in overflow.extras:
        assert f"“{title}”" in headed, title
