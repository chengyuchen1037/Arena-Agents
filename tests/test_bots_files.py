"""``app.storage.botfiles``: the formats of a bot's files (contract §13.2, plan §2.2 / §2.3).

L0 merged markdown (parse leniently / render canonically, every fault named in Chinese), the files of ``core/``, the
playbook, the impressions, the free-text parts, ``record.json``; atomic reads and writes."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from app.bots.types import (
    CORE_EXTRA_CHARS,
    CORE_EXTRA_MAX,
    CORE_LIMITS,
    IMPRESSION_CHARS,
    MEMORY_PART_LIMITS,
    PEOPLE_MAX,
    PLAYBOOK_ENTRY_CHARS,
    PLAYBOOK_MAX,
    SLUG_RE,
    CoreFiles,
    PlaybookEntry,
    core_hash,
)
from app.storage import botfiles as bf
from app.storage.roster import RecordTally

CANONICAL = (
    "---\n"
    "format: arena-core\n"
    "version: 1\n"
    "name: Alice\n"
    "---\n"
    "# 身份\n"
    "一个打了很多年线下局的老玩家，话不多，但一开口就是结论。\n"
    "\n"
    "# 说话方式\n"
    "你说话短，通常两三句话就停，很少超过一百五十字。爱反问，喜欢直接点名。\n"
    "「你急什么？」「这票你自己信吗？」「我只看他投了谁。」\n"
    "篇幅：60–150 字\n"
    "\n"
    "# 分析方式\n"
    "你先看发生了什么，再听谁说了什么。说得漂亮不等于是真的。\n"
    "你相信的东西都只是暂时的，被结果打脸就改。\n"
    "\n"
    "# 附加：口头禅\n"
    "「行，听你的，出了再说。」\n"
)

CORE = CoreFiles(
    identity="一个打了很多年线下局的老玩家，话不多，但一开口就是结论。",
    voice="你说话短，通常两三句话就停，很少超过一百五十字。爱反问，喜欢直接点名。\n「你急什么？」「这票你自己信吗？」「我只看他投了谁。」\n篇幅：60–150 字",
    mind="你先看发生了什么，再听谁说了什么。说得漂亮不等于是真的。\n你相信的东西都只是暂时的，被结果打脸就改。",
    extras=(("口头禅", "「行，听你的，出了再说。」"),),
)


def merged(identity: str = "身份文字", voice: str = "说话文字", mind: str = "分析文字", extras: tuple[tuple[str, str], ...] = (), header: str | None = None) -> str:
    head = header if header is not None else "---\nformat: arena-core\nversion: 1\n---\n"
    text = head + f"# 身份\n{identity}\n\n# 说话方式\n{voice}\n\n# 分析方式\n{mind}\n"
    for title, body in extras:
        text += f"\n# 附加：{title}\n{body}\n"
    return text


def problems_of(text: str, **kwargs) -> list[str]:
    with pytest.raises(bf.BotFileError) as info:
        bf.parse_core_markdown(text, **kwargs)
    return info.value.problems


# ======================================================================================================================
# L0 merged markdown
# ======================================================================================================================
def test_canonical_text_round_trips_byte_for_byte():
    parsed = bf.parse_core_markdown(CANONICAL)
    assert parsed.name == "Alice"
    assert parsed.core == CORE
    rendered = bf.render_core_markdown(parsed.core, parsed.name)
    assert rendered == CANONICAL
    assert rendered.encode("utf-8") == CANONICAL.encode("utf-8")
    # ... and rendering what a core renders to is a fixed point
    again = bf.parse_core_markdown(rendered)
    assert bf.render_core_markdown(again.core, again.name) == rendered


def test_the_example_of_the_plan_is_understood_including_its_comment():
    text = CANONICAL.replace("name: Alice\n", "name: Alice            # 只作提示，导入时不改名\n")
    parsed = bf.parse_core_markdown(text)
    assert parsed.name == "Alice" and parsed.core == CORE
    # (not canonical: the comment is not written back)
    assert bf.render_core_markdown(parsed.core, parsed.name) == CANONICAL


def test_lenient_input_gives_the_same_core():
    messy = (
        "\ufeff\r\n\r\n---  \r\n"
        "# a comment line of the header\r\n"
        "Format：Arena-Core\r\n"
        "ＶＥＲＳＩＯＮ： 1\r\n"
        "unknown: key is ignored\r\n"
        "name：  Alice # not the real name\r\n"
        "---\r\n"
        "\r\n"
        "  # 分析方式  \r\n"
        + CORE.mind.replace("\n", "\r\n")
        + "\r\n\r\n\r\n"
        "#　说话方式\r\n"
        + CORE.voice.replace("\n", "\r\n")
        + "\r\n"
        "# 附加:口头禅\r\n"
        "「行，听你的，出了再说。」\r\n\r\n"
        "# 身份\r\n"
        + CORE.identity
    )
    parsed = bf.parse_core_markdown(messy)
    assert parsed.name == "Alice"
    assert (parsed.core.identity, parsed.core.voice, parsed.core.mind, parsed.core.extras) == (CORE.identity, CORE.voice, CORE.mind, CORE.extras)
    assert bf.render_core_markdown(parsed.core, parsed.name) == CANONICAL  # the order of the sections is the canonical one again


def test_deeper_headings_and_odd_lines_are_part_of_the_text():
    text = merged("## 小标题\n正文\n#不是标题\n---\n    # 缩进四格不是标题", "语气", "方式")
    parsed = bf.parse_core_markdown(text)
    assert parsed.core.identity == "## 小标题\n正文\n#不是标题\n---\n    # 缩进四格不是标题"
    assert bf.render_core_markdown(parsed.core) == bf.render_core_markdown(bf.parse_core_markdown(bf.render_core_markdown(parsed.core)).core)


def test_the_name_in_the_header_is_only_a_hint():
    assert bf.parse_core_markdown(merged()).name is None
    assert bf.parse_core_markdown(merged(header='---\nformat: arena-core\nversion: 1\nname: "Bob"\n---\n')).name == "Bob"
    assert bf.parse_core_markdown(merged(header="---\nformat: arena-core\nversion: 1\nname:\n---\n")).name is None
    assert "name:" not in bf.render_core_markdown(CORE)
    assert "name: Alice#2\n" in bf.render_core_markdown(CORE, "Alice#2")  # a hash inside a word is not a comment
    assert "name: Alice\n" in bf.render_core_markdown(CORE, "Alice # nope\nsecond line")  # nothing in a name can break the header
    parsed = bf.parse_core_markdown(bf.render_core_markdown(CORE, "Alice#2"))
    assert parsed.name == "Alice#2"


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("", "缺少文件头"),
        ("   \n\n", "缺少文件头"),
        ("# 身份\n一\n\n# 说话方式\n二\n\n# 分析方式\n三\n", "缺少文件头"),
        ("---\nformat: arena-core\nversion: 1\n# 身份\n一\n", "文件头没有结束"),
        (merged(header="---\nformat: arena-core\n---\n"), "文件头缺少 version: 1"),
        (merged(header="---\nversion: 1\n---\n"), "文件头缺少 format: arena-core"),
        (merged(header="---\nformat: markdown\nversion: 1\n---\n"), "文件头的 format 必须是 arena-core（现在是「markdown」）"),
        (merged(header="---\nformat: arena-core\nversion: 2\n---\n"), "文件头的 version 目前只能是 1（现在是「2」）"),
        (merged(header="---\nformat: arena-core\nversion: one\n---\n"), "文件头的 version 目前只能是 1（现在是「one」）"),
        ("---\n---\n# 身份\n一\n", "缺少 format"),
        (merged() + "\n# 口头禅\n再来一段\n", "不认识的分区标题「口头禅」"),
        (merged() + "\n#\n空标题\n", "不认识的分区标题「（空）」"),
        (merged() + "\n# 身份\n第二个身份\n", "「身份」分区出现了 2 次，只能有 1 个"),
        (merged(voice="") + "\n# 说话方式\n重复\n", "「说话方式」分区出现了 2 次"),
        ("---\nformat: arena-core\nversion: 1\n---\n# 身份\n一\n\n# 说话方式\n二\n", "缺少「分析方式」分区"),
        ("---\nformat: arena-core\nversion: 1\n---\n# 说话方式\n二\n\n# 分析方式\n三\n", "缺少「身份」分区"),
        (merged(mind=""), "「分析方式」不能为空"),
        (merged(identity="  \n\n  "), "「身份」不能为空"),
        (merged() + "\n# 附加：\n没有标题\n", "附加分区需要标题"),
        (merged(extras=(("口头禅", "一"), ("口头禅", "二"))), "「附加：口头禅」分区出现了 2 次，只能有 1 个"),
        (merged(extras=(("口头禅", ""),)), "「附加：口头禅」不能为空"),
        ("前言\n" + merged(), "缺少文件头"),
        ("---\nformat: arena-core\nversion: 1\n---\n前言\n# 身份\n一\n\n# 说话方式\n二\n\n# 分析方式\n三\n", "第一个分区标题之前有多余的内容"),
        (merged("有\x00空字符"), "不能包含空字符"),
        (merged("x" * 21_000), "文件太大了"),
    ],
)
def test_a_merged_file_that_is_not_acceptable_is_refused_in_chinese(text, fragment):
    problems = problems_of(text)
    assert any(fragment in problem for problem in problems), problems
    assert all(problem.endswith(("。", "）")) or "：" in problem for problem in problems)


def test_a_section_over_its_limit_is_named_with_both_numbers():
    voice = "说" * (CORE_LIMITS["voice"] + 12)
    assert problems_of(merged(voice=voice)) == ["「说话方式」最多 600 字（现在 612 字）。"]
    assert problems_of(merged("身" * 301)) == ["「身份」最多 300 字（现在 301 字）。"]
    assert problems_of(merged(mind="分" * 401)) == ["「分析方式」最多 400 字（现在 401 字）。"]
    assert problems_of(merged(extras=(("口头禅", "禅" * (CORE_EXTRA_CHARS + 1)),))) == ["「附加：口头禅」最多 300 字（现在 301 字）。"]
    # exactly at the limit is fine; the text is counted without the blank lines around it
    ok = bf.parse_core_markdown(merged("身" * 300, "说" * 600, "分" * 400, extras=(("附", "加" * 300),)).replace("# 身份\n", "# 身份\n\n\n"))
    assert len(ok.core.identity) == 300 and len(ok.core.voice) == 600 and len(ok.core.extras[0][1]) == 300


def test_only_three_extra_sections_are_allowed():
    four = merged(extras=tuple((f"附加{i}", "文字") for i in range(1, CORE_EXTRA_MAX + 2)))
    assert problems_of(four) == ["附加分区最多 3 份（现在 4 份）。"]
    three = bf.parse_core_markdown(merged(extras=tuple((f"附加{i}", "文字") for i in range(1, CORE_EXTRA_MAX + 1))))
    assert [title for title, _ in three.core.extras] == ["附加1", "附加2", "附加3"]


def test_every_fault_is_reported_together_in_the_order_of_the_file():
    text = (
        "---\nformat: arena-core\nversion: 3\n---\n"
        "# 身份\n一\n\n# 说话方式\n" + "说" * 612 + "\n\n# 别的\n文字\n"
    )
    problems = problems_of(text)
    assert problems[0].startswith("文件头的 version")
    assert any("不认识的分区标题「别的」" in p for p in problems)
    assert "缺少「分析方式」分区。" in problems
    assert "「说话方式」最多 600 字（现在 612 字）。" in problems
    assert problems.index("缺少「分析方式」分区。") < problems.index("「说话方式」最多 600 字（现在 612 字）。")
    with pytest.raises(bf.BotFileError) as info:
        bf.parse_core_markdown(text)
    assert str(info.value) == " ".join(problems)  # the message that is shown as it is


def test_an_incomplete_core_renders_as_a_template_that_is_refused_until_it_is_filled_in():
    template = bf.render_core_markdown(CoreFiles(identity="一句话"), "Alice")
    assert template == "---\nformat: arena-core\nversion: 1\nname: Alice\n---\n# 身份\n一句话\n\n# 说话方式\n\n# 分析方式\n"
    assert problems_of(template) == ["「说话方式」不能为空。", "「分析方式」不能为空。"]
    parsed = bf.parse_core_markdown(template, require_complete=False)
    assert parsed.core == CoreFiles(identity="一句话") and parsed.core.missing == ("voice", "mind")
    # the limits still apply when completeness is not asked for
    assert problems_of(merged("身" * 301), require_complete=False) == ["「身份」最多 300 字（现在 301 字）。"]
    assert bf.render_core_markdown(CoreFiles()).endswith("# 身份\n\n# 说话方式\n\n# 分析方式\n")


def test_validate_core_names_every_fault():
    core = CoreFiles(
        identity="一\n# 标题行\n二",
        voice="说" * 601,
        mind="",
        extras=(("口头禅", ""), ("口头禅", "重复"), ("", "无标题"), ("很长的标题" * 6, "x"), ("四", "x")),
    )
    problems = bf.validate_core(core, require_complete=True)
    assert "「身份」里不能有以“# ”开头的行（会被当成分区标题），请去掉 # 或改用 ##。" in problems
    assert "「说话方式」最多 600 字（现在 601 字）。" in problems
    assert "「分析方式」不能为空。" in problems
    assert "附加分区最多 3 份（现在 5 份）。" in problems
    assert "「附加：口头禅」不能为空。" in problems and "附加分区「口头禅」重复了。" in problems
    assert "附加分区需要标题。" in problems and any("标题最多 20 字" in p for p in problems)
    assert bf.validate_core(CORE) == [] and bf.validate_core(CORE, require_complete=True) == []
    assert bf.validate_core(CoreFiles()) == [] and len(bf.validate_core(CoreFiles(), require_complete=True)) == 3


# ======================================================================================================================
# slugs
# ======================================================================================================================
@pytest.mark.parametrize(
    "title,taken,index,expected",
    [
        ("Catch Phrase 1", (), 1, "catchphrase1"),
        ("口头禅", (), 1, "x1"),
        ("口头禅", (), 2, "x2"),
        ("口头禅 (v2)", (), 1, "v2"),
        ("habit", ("habit",), 1, "habit-2"),
        ("habit", ("habit", "habit-2"), 1, "habit-3"),
        ("口头禅", ("x1",), 1, "x1-2"),
        ("---abc---", (), 1, "abc"),
        ("---", (), 3, "x3"),
        ("😀😀", (), 1, "x1"),
        ("A_B", (), 1, "ab"),
        ("1", (), 1, "1"),
        ("a" * 50, (), 1, "a" * 32),
        ("a" * 50, ("a" * 32,), 1, "a" * 30 + "-2"),
    ],
)
def test_the_slug_of_an_extra_section(title, taken, index, expected):
    slug = bf.slug_for_title(title, taken, index)
    assert slug == expected
    assert SLUG_RE.match(slug)


def test_slugs_are_kept_for_titles_that_already_have_a_file():
    titles = ["口头禅", "Habit", "备注"]
    assert bf.assign_slugs(titles) == ["x1", "habit", "x3"]
    assert bf.assign_slugs(titles, {"备注": "x2", "habit": "old-habit"}) == ["x1", "old-habit", "x2"]
    assert bf.assign_slugs(["备注"], {"备注": "x2"}) == ["x2"]
    assert len(set(bf.assign_slugs(["口头禅", "口头禅", "口头禅"]))) == 3  # a repeated title never overwrites a file


# ======================================================================================================================
# the files of core/
# ======================================================================================================================
def test_core_files_round_trip_and_look_like_the_contract(tmp_path: Path):
    core_dir = tmp_path / "core"
    slugs = bf.write_core(core_dir, CORE)
    assert slugs == ("x1",)
    assert (core_dir / "identity.md").read_text(encoding="utf-8") == CORE.identity + "\n"
    assert (core_dir / "voice.md").read_text(encoding="utf-8") == CORE.voice + "\n"
    assert (core_dir / "mind.md").read_text(encoding="utf-8") == CORE.mind + "\n"
    assert (core_dir / "extra-x1.md").read_text(encoding="utf-8") == "# 口头禅\n\n「行，听你的，出了再说。」\n"  # first line = the title
    disk = bf.read_core_on_disk(core_dir)
    assert disk.core == CORE and disk.slugs == ("x1",) and disk.updated_at is not None
    assert core_hash(disk.core) == core_hash(CORE)  # the hash follows the text, not the layout
    # the merged file and the files are two views of the same thing
    merged_text = bf.render_core_markdown(disk.core, "Alice")
    assert merged_text == CANONICAL
    other = tmp_path / "other"
    bf.write_core(other, bf.parse_core_markdown(merged_text).core)
    assert bf.read_core(other) == CORE
    assert not list(core_dir.glob("*.tmp")) and not list(core_dir.glob(".*"))  # no temporary file is left behind


def test_an_empty_directory_is_an_empty_core(tmp_path: Path):
    disk = bf.read_core_on_disk(tmp_path / "nothing")
    assert disk.core == CoreFiles() and disk.slugs == () and disk.updated_at is None
    assert disk.core.missing == ("identity", "voice", "mind")


def test_writing_a_core_removes_what_is_no_longer_there(tmp_path: Path):
    core_dir = tmp_path / "core"
    bf.write_core(core_dir, CoreFiles("a", "b", "c", (("口头禅", "x"), ("Habit", "y"))))
    assert sorted(p.name for p in core_dir.iterdir()) == ["extra-habit.md", "extra-x1.md", "identity.md", "mind.md", "voice.md"]
    (core_dir / "notes.txt").write_text("not ours", encoding="utf-8")
    (core_dir / "extra-BAD_SLUG.md").write_text("# ignored\n\ninvalid slug", encoding="utf-8")
    bf.write_core(core_dir, CoreFiles("a2", "", "c2", (("Habit", "y2"),)))
    assert sorted(p.name for p in core_dir.iterdir()) == ["extra-BAD_SLUG.md", "extra-habit.md", "identity.md", "mind.md", "notes.txt"]
    disk = bf.read_core_on_disk(core_dir)
    assert disk.core == CoreFiles("a2", "", "c2", (("Habit", "y2"),)) and disk.slugs == ("habit",)
    bf.clear_core(core_dir)
    assert sorted(p.name for p in core_dir.iterdir()) == ["extra-BAD_SLUG.md", "notes.txt"]  # only the L0 is cleared
    assert bf.read_core(core_dir) == CoreFiles()


def test_an_extra_keeps_its_file_when_others_come_and_go(tmp_path: Path):
    core_dir = tmp_path / "core"
    bf.write_core(core_dir, CoreFiles("a", "b", "c", (("口头禅", "一"), ("备注", "二"))))
    assert bf.read_core_on_disk(core_dir).slugs == ("x1", "x2")
    bf.write_core(core_dir, CoreFiles("a", "b", "c", (("备注", "二"),)))  # the first one is gone
    assert bf.read_core_on_disk(core_dir).slugs == ("x2",)  # (the second keeps x2: a proposal that names it still finds it)
    bf.write_core(core_dir, CoreFiles("a", "b", "c", (("新的", "三"), ("备注", "二"))))
    assert bf.read_core_on_disk(core_dir).slugs == ("x1", "x2")


def test_a_refused_core_changes_nothing(tmp_path: Path):
    core_dir = tmp_path / "core"
    bf.write_core(core_dir, CORE)
    before = {p.name: p.read_bytes() for p in core_dir.iterdir()}
    with pytest.raises(bf.BotFileError) as info:
        bf.write_core(core_dir, CoreFiles("新身份", "说" * 601, "新方式", (("口头禅", "新"),)))
    assert info.value.problems == ["「说话方式」最多 600 字（现在 601 字）。"]
    with pytest.raises(bf.BotFileError):
        bf.write_core(core_dir, CoreFiles("a\x00b", "v", "m"))
    assert {p.name: p.read_bytes() for p in core_dir.iterdir()} == before


def test_hand_edited_files_are_read_leniently(tmp_path: Path):
    core_dir = tmp_path / "core"
    core_dir.mkdir()
    (core_dir / "identity.md").write_bytes(b"\xef\xbb\xbf\xe8\xba\xab\xe4\xbb\xbd\r\n\xe7\xac\xac\xe4\xba\x8c\xe8\xa1\x8c\r\n\r\n")  # BOM, CRLF, blank lines
    (core_dir / "voice.md").write_text("   \n\n", encoding="utf-8")  # blank = empty
    (core_dir / "extra-nohead.md").write_text("没有标题行\n第二行\n", encoding="utf-8")  # no "# title": the slug is the title
    (core_dir / "extra-a.md").write_text("\n\n# 带标题\n\n正文\r\n", encoding="utf-8")
    (core_dir / "extra-b").mkdir()  # a directory is not an extra
    disk = bf.read_core_on_disk(core_dir)
    assert disk.core.identity == "身份\n第二行"
    assert disk.core.voice == "" and disk.core.missing == ("voice", "mind")
    assert disk.slugs == ("a", "nohead")
    assert disk.core.extras == (("带标题", "正文"), ("nohead", "没有标题行\n第二行"))


def test_sections_are_replaced_one_at_a_time(tmp_path: Path):
    core_dir = tmp_path / "core"
    bf.write_core(core_dir, CORE)
    disk = bf.read_core_on_disk(core_dir)
    assert bf.core_section_text(disk, "voice") == CORE.voice
    assert bf.core_section_text(disk, "extra:x1") == CORE.extras[0][1]
    assert bf.core_section_text(disk, "extra:口头禅") == CORE.extras[0][1]
    assert bf.core_section_text(disk, "extra:不存在") == ""
    after = bf.apply_section(disk, "voice", "新的说话方式\r\n")
    assert after.voice == "新的说话方式" and after.identity == CORE.identity and after.extras == CORE.extras
    by_slug = bf.apply_section(disk, "extra:x1", "改")
    by_title = bf.apply_section(disk, "extra:口头禅", "改")
    assert by_slug == by_title and by_slug.extras == (("口头禅", "改"),)
    added = bf.apply_section(disk, "extra:新分区", "新")
    assert added.extras == (("口头禅", CORE.extras[0][1]), ("新分区", "新"))
    full = CoreFiles("a", "b", "c", (("一", "x"), ("二", "x"), ("三", "x")))
    bf.write_core(tmp_path / "full", full)
    with pytest.raises(bf.BotFileError) as info:
        bf.apply_section(bf.read_core_on_disk(tmp_path / "full"), "extra:四", "x")
    assert "最多 3 份" in str(info.value)
    assert bf.apply_section(bf.read_core_on_disk(tmp_path / "full"), "extra:二", "改").extras[1] == ("二", "改")  # an existing one is fine
    for bad in ("", "other", "extra:", "extra", "Identity"):
        with pytest.raises(bf.BotFileError):
            bf.split_section(bad)
    assert bf.split_section("mind") == ("mind", "") and bf.split_section("extra: 口头禅 ") == ("extra", "口头禅")


def test_reading_never_sees_a_half_written_core(tmp_path: Path):
    """One writer alternates between two cores, a reader keeps reading: every read is one of them, whole."""
    core_dir = tmp_path / "core"
    a = CoreFiles("甲身份", "甲说话", "甲方式", (("甲", "甲附加"),))
    b = CoreFiles("乙身份", "乙说话", "乙方式", (("乙", "乙附加"),))
    bf.write_core(core_dir, a)
    seen: list[CoreFiles] = []
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                seen.append(bf.read_core(core_dir))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for i in range(40):
            bf.write_core(core_dir, b if i % 2 == 0 else a)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not errors, errors
    assert seen and all(core in (a, b) for core in seen)


# ======================================================================================================================
# playbook.md
# ======================================================================================================================
PLAYBOOK_TEXT = (
    "## 假设\n"
    "- [把握:中|支持:3|反例:1|来源:复盘 2026-11-20|#对跳|#投票] 两个人都说自己是同一个身份时，先看谁的结论和之后的行动对得上。\n"
    "- [把握:低|支持:0|反例:0|来源:初始化|#警上] 首夜验谁本来就没有道理可讲，追问这个得不到信息。\n"
    "- [把握:高|支持:7|反例:0] 没有来源也没有标签的一条。\n"
)


def test_a_playbook_round_trips_and_the_canonical_text_is_a_fixed_point():
    entries = bf.parse_playbook(PLAYBOOK_TEXT)
    assert entries == [
        PlaybookEntry("两个人都说自己是同一个身份时，先看谁的结论和之后的行动对得上。", "中", 3, 1, "复盘 2026-11-20", ("对跳", "投票")),
        PlaybookEntry("首夜验谁本来就没有道理可讲，追问这个得不到信息。", "低", 0, 0, "初始化", ("警上",)),
        PlaybookEntry("没有来源也没有标签的一条。", "高", 7, 0, "", ()),
    ]
    assert bf.render_playbook(entries) == PLAYBOOK_TEXT
    assert bf.render_playbook(bf.parse_playbook(bf.render_playbook(entries))) == PLAYBOOK_TEXT
    assert bf.render_playbook([]) == "" and bf.parse_playbook("") == [] and bf.parse_playbook(None) == []


def test_a_playbook_is_read_leniently():
    text = (
        "\ufeff# 打法\r\n"
        "前面有一句话，不是条目。\r\n"
        "## 假设\r\n"
        "* ［把握：中｜支持：2｜反例：1｜来源：复盘｜＃投票］ 全角括号和冒号也认。\r\n"
        "1. [低] 只有把握度。\r\n"
        "- [把握:很高|支持:x|反例:-3|来源:|未知字段:1|#a b|#a b|#] 把握度写成“很高”：取第一个字；支持写不出数字就是 0。\r\n"
        "- 没有方括号的一条。\r\n"
        "- [把握:medium] english word for the confidence.\r\n"
        "- [把握:中|支持:1|反例:0]   \r\n"
        "[把握:高] 没有项目符号也行。\r\n"
        "\r\n"
        "## 备注\r\n"
        "- [把握:高] 另一个小节里的不是条目。\r\n"
    )
    entries, ignored = bf.parse_playbook_ex(text)
    assert [e.text for e in entries] == [
        "全角括号和冒号也认。",
        "只有把握度。",
        "把握度写成“很高”：取第一个字；支持写不出数字就是 0。",
        "没有方括号的一条。",
        "english word for the confidence.",
        "没有项目符号也行。",
    ]
    assert [e.confidence for e in entries] == ["中", "低", "高", "低", "中", "高"]
    assert (entries[0].support, entries[0].counter, entries[0].source, entries[0].tags) == (2, 1, "复盘", ("投票",))
    assert (entries[2].support, entries[2].counter, entries[2].tags) == (0, 0, ("ab",))  # a tag has no spaces; the same tag once
    assert ignored == 1  # the entry without text ("- [把握:中|支持:1|反例:0]") counts; headings and the other section do not
    # what comes back from the canonical text is what was understood
    assert bf.parse_playbook(bf.render_playbook(entries)) == entries


def test_special_characters_cannot_break_a_playbook_line():
    entry = PlaybookEntry("正文里可以有 | 和 ] 和 [方括号]，也可以以 # 开头", "中", 1, 2, "复盘 | 第 3 局 [x]", ("a|b", "#c", "d e", "", "f" * 30))
    text = bf.render_playbook([entry])
    assert len(text.strip().splitlines()) == 2  # heading + one line
    (back,) = bf.parse_playbook(text)
    assert back.text == entry.text
    assert back.source == "复盘 / 第 3 局 (x)"
    assert back.tags == ("ab", "c", "de", "f" * 20)
    assert bf.render_playbook([back]) == text  # fixed point
    multi = PlaybookEntry("第一行\n第二行\t后面", "高")
    assert bf.parse_playbook(bf.render_playbook([multi]))[0].text == "第一行 第二行 后面"
    assert bf.normalize_entry(PlaybookEntry("x", "奇怪", -4, -1)).confidence == "低"
    assert bf.normalize_entry(PlaybookEntry("x", "奇怪", -4, -1)).support == 0


def test_the_playbook_limits_are_named():
    ok = [PlaybookEntry(f"第 {i} 条", "低") for i in range(PLAYBOOK_MAX)]
    assert bf.check_playbook(ok) == []
    assert bf.check_playbook(ok + [PlaybookEntry("多的一条")]) == ["打法最多 20 条（现在 21 条）。"]
    long_entry = PlaybookEntry("长" * (PLAYBOOK_ENTRY_CHARS + 1))
    (problem,) = bf.check_playbook([PlaybookEntry("短"), long_entry])
    assert problem.startswith("打法第 2 条最多 80 字（现在 81 字）：")
    assert bf.check_playbook([PlaybookEntry("长" * PLAYBOOK_ENTRY_CHARS)]) == []
    assert bf.check_playbook([PlaybookEntry("   ")]) == ["打法第 1 条没有内容。"]


# ======================================================================================================================
# people.md
# ======================================================================================================================
def test_impressions_round_trip_and_are_read_leniently():
    text = "- Bob：话少，爱跟票。\n- Carol：爱带节奏。\n"
    assert bf.parse_people(text) == {"Bob": "话少，爱跟票。", "Carol": "爱带节奏。"}
    assert bf.render_people(bf.parse_people(text)) == text
    messy = "\ufeff## 印象\r\nBob:话少\r\n* Carol ： 爱带节奏\r\n3. Dave：  话  很多\r\n- Eve：（暂无）\r\n- Frank：\r\n这行没有冒号\r\n：没有名字\r\n- bob：改口了\r\n"
    people, ignored = bf.parse_people_ex(messy)
    assert people == {"Bob": "改口了", "Carol": "爱带节奏", "Dave": "话 很多"}  # later wins (any case), at the place of the first
    assert list(people) == ["Bob", "Carol", "Dave"]
    assert ignored == 2  # the line without a colon and the one without a name; the empty impressions are simply not entries
    assert bf.render_people({}) == "" and bf.parse_people("") == {} and bf.render_people({"Bob": "", " ": "x"}) == ""


def test_the_impression_limits_are_named():
    people = {f"玩家{i}": "印象" for i in range(PEOPLE_MAX)}
    assert bf.check_people(people) == []
    assert bf.check_people({**people, "多的人": "印象"}) == ["印象最多 30 人（现在 31 人）。"]
    assert bf.check_people({"Bob": "话" * (IMPRESSION_CHARS + 5)}) == ["对「Bob」的印象最多 80 字（现在 85 字）。"]
    assert bf.check_people({"Bob": "话" * IMPRESSION_CHARS}) == []
    bad = bf.check_people({'B"ob': "x", "": "y"})
    assert len(bad) == 2 and all(p.startswith("印象里的名字") for p in bad)


# ======================================================================================================================
# the parts of the memory on disk
# ======================================================================================================================
def test_every_part_lives_where_the_contract_says(tmp_path: Path):
    bot = tmp_path / "b-1a2b3c4d"
    assert bf.part_relpath("arch_notes") == "arch/notes.md"
    assert bf.part_relpath("people") == "people.md"
    assert bf.part_relpath("digest", "werewolf_12p") == "rules/werewolf_12p/digest.md"
    assert bf.part_relpath("playbook", "demo") == "rules/demo/playbook.md"
    assert bf.part_relpath("questions", "demo") == "rules/demo/questions.md"
    for part, content in {
        "arch_notes": "我的操作笔记",
        "digest": "我的规则要点",
        "playbook": PLAYBOOK_TEXT,
        "questions": "还不确定…",
        "people": "- Bob：话少\n",
    }.items():
        result = bf.write_part(bot, part, content, "demo")
        assert result.ok and result.file == part and result.chars == len(bf.read_part(bot, part, "demo")), result
        assert bf.read_part(bot, part, "demo") == content.strip()
    assert (bot / "arch" / "notes.md").read_text(encoding="utf-8") == "我的操作笔记\n"
    assert (bot / "rules" / "demo" / "playbook.md").read_text(encoding="utf-8") == PLAYBOOK_TEXT
    assert (bot / "people.md").read_text(encoding="utf-8") == "- Bob：话少\n"
    assert bf.part_stats(bot, "demo") == {"arch_notes": 6, "digest": 6, "playbook": 3, "questions": 5, "people": 1}
    assert bf.part_stats(bot) == {"arch_notes": 6, "digest": 0, "playbook": 0, "questions": 0, "people": 1}
    # another scenario has its own L2 (but shares the notes and the impressions)
    assert bf.read_part(bot, "digest", "other") == "" and bf.part_stats(bot, "other")["arch_notes"] == 6


def test_a_written_part_is_the_canonical_text(tmp_path: Path):
    bot = tmp_path / "bot"
    result = bf.write_part(bot, "playbook", "- [把握：中] 只写了一半的格式\n无关的一句话\n", "demo")
    assert result.ok and "1 条" in result.message and "有 1 行没有读出，已忽略" in result.message
    assert bf.read_part(bot, "playbook", "demo") == "## 假设\n- [把握:中|支持:0|反例:0] 只写了一半的格式"
    result = bf.write_part(bot, "people", "Bob:话少\n* Carol：爱带节奏\n", "demo")
    assert result.ok and "2 人" in result.message
    assert bf.read_part(bot, "people") == "- Bob：话少\n- Carol：爱带节奏"
    assert bf.write_part(bot, "digest", "  \r\n正文\r\n第二行\r\n\r\n", "demo").chars == len("正文\n第二行")


def test_a_refused_part_names_the_part_the_limit_and_the_number_and_changes_nothing(tmp_path: Path):
    bot = tmp_path / "bot"
    assert bf.write_part(bot, "digest", "旧的要点", "demo").ok
    result = bf.write_part(bot, "digest", "字" * (MEMORY_PART_LIMITS["digest"] + 12), "demo")
    assert not result.ok and result.file == "digest"
    assert result.message.startswith("「规则要点」最多 800 字（现在 812 字）。")
    assert bf.read_part(bot, "digest", "demo") == "旧的要点"
    assert bf.write_part(bot, "arch_notes", "字" * 801, "demo").message.startswith("「操作笔记」最多 800 字（现在 801 字）。")
    assert bf.write_part(bot, "questions", "字" * 401, "demo").message.startswith("「待解问题」最多 400 字（现在 401 字）。")
    too_many = "".join(f"- [把握:低] 第 {i} 条\n" for i in range(PLAYBOOK_MAX + 1))
    assert bf.write_part(bot, "playbook", too_many, "demo").message.startswith("打法最多 20 条（现在 21 条）。")
    assert bf.write_part(bot, "playbook", "只是一段话，没有条目", "demo").message.startswith("没有读出任何一条打法")
    assert bf.write_part(bot, "people", "没有冒号的一行", "demo").message.startswith("没有读出任何一条印象")
    assert bf.write_part(bot, "digest", "有\x00空字符", "demo").message.startswith("规则要点不能包含空字符")
    assert bf.write_part(bot, "digest", "孤立代理\ud800字符", "demo").ok is False
    assert bf.write_part(bot, "nonsense", "x", "demo").message.startswith("不认识的记忆部分")
    assert not (bot / "rules" / "demo" / "playbook.md").exists() and not (bot / "people.md").exists()
    for bad_id in ("", "../x", "a/b", "a\\b", ".hidden", "x" * 65, "带中文"):
        assert not bf.write_part(bot, "digest", "x", bad_id).ok, bad_id
    assert not (tmp_path / "x").exists()
    # an empty text clears the part (not an error)
    cleared = bf.write_part(bot, "digest", "  ", "demo")
    assert cleared.ok and cleared.chars == 0 and not (bot / "rules" / "demo" / "digest.md").exists()
    assert bf.write_part(bot, "people", "", "demo").ok


def test_parts_that_are_hand_edited_are_read_as_they_are(tmp_path: Path):
    bot = tmp_path / "bot"
    (bot / "arch").mkdir(parents=True)
    (bot / "arch" / "notes.md").write_bytes(b"\xef\xbb\xbf\xe7\xac\x94\xe8\xae\xb0\r\n\xe7\xac\xac\xe4\xba\x8c\xe8\xa1\x8c\r\n")
    (bot / "people.md").write_bytes(b"\xff\xfe- Bob\xef\xbc\x9a\xe8\xaf\x9d\xe5\xb0\x91\n")  # not valid UTF-8: replaced, not fatal
    assert bf.read_part(bot, "arch_notes") == "笔记\n第二行"
    assert "话少" in bf.read_text(bot / "people.md")
    assert bf.read_part(bot, "questions", "demo") == ""


def test_parts_written_at_the_same_time_never_corrupt_each_other(tmp_path: Path):
    bot = tmp_path / "bot"
    texts = [f"第 {i} 份要点：" + "字" * 100 for i in range(8)]
    errors: list[BaseException] = []

    def work(text: str) -> None:
        try:
            for _ in range(15):
                assert bf.write_part(bot, "digest", text, "demo").ok
                assert bf.read_part(bot, "digest", "demo") in texts
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(text,)) for text in texts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    assert bf.read_part(bot, "digest", "demo") in texts
    assert sorted(p.name for p in (bot / "rules" / "demo").iterdir()) == ["digest.md"]  # no temporary file is left behind


# ======================================================================================================================
# record.json
# ======================================================================================================================
def test_the_record_is_kept_in_record_json(tmp_path: Path):
    bot = tmp_path / "bot"
    assert bf.read_record(bot).games == 0 and bf.record_text(bf.read_record(bot)) == "" and bf.record_summary(bf.read_record(bot)) == ""
    bf.add_record(bot, "角色甲", True)
    bf.add_record(bot, "角色甲", False)
    tally = bf.add_record(bot, "角色乙", True)
    assert (tally.games, tally.wins, tally.by_role) == (3, 2, {"角色甲": [2, 1], "角色乙": [1, 1]})
    assert json.loads((bot / "record.json").read_text(encoding="utf-8")) == {"games": 3, "wins": 2, "by_role": {"角色乙": [1, 1], "角色甲": [2, 1]}}
    assert bf.record_summary(bf.read_record(bot)) == "场次 3 · 胜 2 · 胜率 67%"
    assert bf.record_text(bf.read_record(bot)) == "场次 3，胜 2，负 1，胜率 67%\n- 角色乙：1 场，胜 1\n- 角色甲：2 场，胜 1"
    unknown = bf.add_record(bot, "角色甲", None)  # an outcome nobody knows leaves the record alone
    assert unknown.games == 3
    assert bf.add_record(bot, "  ", True).by_role["未知身份"] == [1, 1]


def test_a_broken_record_is_an_empty_record(tmp_path: Path):
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "record.json").write_text("not json", encoding="utf-8")
    assert bf.read_record(bot).games == 0
    (bot / "record.json").write_text('["a list"]', encoding="utf-8")
    assert bf.read_record(bot).games == 0
    (bot / "record.json").write_text('{"games": "7", "wins": 99, "by_role": {"甲": [2, 5], "乙": "x", "": [1, 1], "丙": [-1, 3]}}', encoding="utf-8")
    tally = bf.read_record(bot)
    assert (tally.games, tally.wins) == (7, 7) and tally.by_role == {"甲": [2, 2], "丙": [0, 0]}  # wins never exceed games
    bf.write_record(bot, RecordTally())
    assert not (bot / "record.json").exists()


def test_two_games_that_finish_at_once_both_count(tmp_path: Path):
    bot = tmp_path / "bot"
    threads = [threading.Thread(target=lambda: [bf.add_record(bot, "角色", True) for _ in range(10)]) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert bf.read_record(bot).games == 50


def test_a_v0_13_record_text_is_understood():
    tally = bf.tally_from_text("场次 5，胜 3，负 2，胜率 60%\n- 角色甲：3 场，胜 2\n- 角色乙：2 场，胜 1")
    assert (tally.games, tally.wins, tally.by_role) == (5, 3, {"角色甲": [3, 2], "角色乙": [2, 1]})
    assert bf.tally_from_text("").games == 0


# ======================================================================================================================
# situation tags (the scenario's ``situations``): a hint, never a fault
# ======================================================================================================================
def test_a_playbook_tag_the_scenario_does_not_know_is_written_and_only_mentioned(tmp_path: Path):
    bot = tmp_path / "bot"
    text = "- [把握:中|#投票|#没有这个] 甲\n- [把握:低|#发言|#没有这个|#另一个] 乙\n- [把握:低] 没有标签的一条\n"
    known = ["警上", "发言", "投票", "夜晚"]
    result = bf.write_part(bot, "playbook", text, "demo", known_tags=known)
    assert result.ok
    assert result.message.endswith("有标签不在这个剧本的情境标签里，按它检索不到：#没有这个、#另一个。")
    stored = bf.parse_playbook(bf.read_part(bot, "playbook", "demo"))
    assert [e.tags for e in stored] == [("投票", "没有这个"), ("发言", "没有这个", "另一个"), ()]  # (written as they are)
    # nothing to say when every tag is known, when there are none, or when the scenario's tags are not given
    assert "标签" not in bf.write_part(bot, "playbook", "- [把握:中|#投票] 甲", "demo", known_tags=known).message
    assert "标签" not in bf.write_part(bot, "playbook", "- [把握:中] 甲", "demo", known_tags=known).message
    assert "标签" not in bf.write_part(bot, "playbook", text, "demo").message
    assert "标签" not in bf.write_part(bot, "playbook", text, "demo", known_tags=["没有这个", "另一个", "投票", "发言"]).message
    # a scenario with no tags at all: every tag is a hint
    assert "#投票、#没有这个" in bf.write_part(bot, "playbook", "- [把握:中|#投票|#没有这个] 甲", "demo", known_tags=[]).message
    # the hint is joined with the note about lines that were not understood
    note = bf.normalize_part("playbook", "- [把握:中|#x] 甲\n一句无关的话", known_tags=[])[1]
    assert note == "有 1 行没有读出，已忽略。有标签不在这个剧本的情境标签里，按它检索不到：#x。"
    assert bf.unknown_tags(bf.parse_playbook(text), known) == ["没有这个", "另一个"] and bf.unknown_tags([], known) == []
    # other parts ignore it
    assert bf.write_part(bot, "digest", "要点", "demo", known_tags=[]).message == "已写入规则要点（2 字）。"
