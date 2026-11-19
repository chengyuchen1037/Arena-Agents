"""v0.13.1 头像编号（G27，契约 §12.6）：``app.avatars.encode_description`` / ``decode_code``。

全部离线。格式层面的向量放在 ``tests/avatar_code_cases.json``（前端 ``static/js/avatar.js`` 的测试读同一个文件，两边
用同一组样例保证编码 / 解码一致）；这里还覆盖：向量文件自身的结构、对照真实素材库的往返与 ``unknown_part``、编码的边界
（``none`` / 空配饰色 / 首字母不对 / 结尾换行）、非字符串输入、中文报错，以及“解码结果能直接交给 ``validate_description``”。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import avatars as av  # noqa: E402

CASES_FILE = ROOT / "tests" / "avatar_code_cases.json"
CASES = json.loads(CASES_FILE.read_text(encoding="utf-8"))
FIELDS = av.FIELDS
CJK = re.compile(r"[\u4e00-\u9fff]")
CANONICAL = re.compile(r"(?:[fshcemak][0-9]{2,3}){8}")
GOOD = {"v": 1, "face": "f03", "skin": "s05", "hair": "h12", "hair_color": "c07", "eyes": "e04", "mouth": "m02", "accessory": "a09", "accessory_color": "k03"}
GOOD_CODE = "f03s05h12c07e04m02a09k03"


@pytest.fixture(scope="module")
def library() -> dict:
    return av.load_parts()


# ------------------------------------------------------------------------------------------------ the shared vector file
def test_the_vector_file_has_the_contract_shape():
    assert set(CASES) >= {"valid", "invalid"} and isinstance(CASES["notes"], list)
    assert len(CASES["valid"]) >= 20 and len(CASES["invalid"]) >= 30
    for case in CASES["valid"]:
        assert set(case) == {"input", "code", "description"}, case
        assert CANONICAL.fullmatch(case["code"]), case  # the canonical form: lower case, no separators, eight ids
        desc = case["description"]
        assert list(desc) == ["v", *FIELDS] and desc["v"] == 1, case
        assert all(desc[f][0] == av.FIELD_LETTERS[f] for f in FIELDS), case
    for case in CASES["invalid"]:
        assert set(case) <= {"input", "reason", "field"}, case
        assert case["reason"] in av.CODE_REASONS and case["reason"] != "unknown_part", case  # unknown_part needs a library
        if case["reason"] in ("missing_field", "duplicate_field"):
            assert case["field"] in FIELDS, case
        else:
            assert "field" not in case, case
    # the vectors exercise every format-level reason, and the 3-digit ids the id regex allows
    assert {c["reason"] for c in CASES["invalid"]} == {"empty", "bad_format", "missing_field", "duplicate_field"}
    assert any(len(c["description"][f]) == 4 for c in CASES["valid"] for f in FIELDS)
    # every missing / duplicate field shows up, so a decoder cannot get away with a fixed answer
    assert {c["field"] for c in CASES["invalid"] if c["reason"] == "missing_field"} == set(FIELDS)


@pytest.mark.parametrize("case", CASES["valid"], ids=lambda c: c["input"].encode("unicode_escape").decode("ascii")[:60])
def test_valid_vectors_decode_encode_and_round_trip(case):
    assert av.decode_code(case["input"]) == case["description"]
    assert av.decode_code(case["code"]) == case["description"]
    assert list(av.decode_code(case["input"])) == ["v", *FIELDS]  # the same key order as validate_description's output
    assert av.encode_description(case["description"]) == case["code"]
    assert av.decode_code(av.encode_description(case["description"])) == case["description"]


@pytest.mark.parametrize("case", CASES["invalid"], ids=lambda c: c["input"].encode("unicode_escape").decode("ascii")[:60])
def test_invalid_vectors_fail_with_the_machine_readable_reason(case):
    with pytest.raises(av.AvatarCodeError) as exc:
        av.decode_code(case["input"])
    err = exc.value
    assert isinstance(err, ValueError) and err.reason == case["reason"], (case, err.reason, str(err))
    assert err.field == case.get("field"), (case, err.field)
    assert err.part is None  # parts are only checked against a library
    assert CJK.search(str(err)), str(err)  # the message is for the user: Chinese
    # a library does not change how format problems are reported
    with pytest.raises(av.AvatarCodeError) as with_lib:
        av.decode_code(case["input"], library=av.load_parts())
    assert (with_lib.value.reason, with_lib.value.field) == (err.reason, err.field)


# ------------------------------------------------------------------------------------------------ decode
def test_the_docstring_example_and_the_contract_example():
    assert av.decode_code(GOOD_CODE) == GOOD
    assert av.encode_description(GOOD) == GOOD_CODE


def test_decode_is_lenient_about_case_separators_and_order_but_not_about_the_ids():
    expected = av.decode_code(GOOD_CODE)
    for text in ("F03S05H12C07E04M02A09K03", "f03 s05 h12 c07 e04 m02 a09 k03", " f03-s05-h12-c07-e04-m02-a09-k03 ",
                 "f03_s05_h12_c07_e04_m02_a09_k03", "f03:s05:h12:c07:e04:m02:a09:k03", "k03 a09 m02 e04 c07 h12 s05 f03"):
        assert av.decode_code(text) == expected, text
    assert av.decode_code("f03s05h12c07e04m02a00k03")["accessory"] == "a00"  # a00 is simply "no accessory"
    for text in ("f03s05h12c07e04m02a09k03.", "f03/s05/h12/c07/e04/m02/a09/k03", "f3s05h12c07e04m02a09k03"):
        with pytest.raises(av.AvatarCodeError) as exc:
            av.decode_code(text)
        assert exc.value.reason == "bad_format", text


def test_decode_reports_the_first_problem_in_a_fixed_order():
    # format beats duplicate beats missing: the same order the shared vectors pin for the JS implementation
    cases = [
        ("f03f04s05h12c07e04m02a09k03k", "bad_format", None),  # a stray ``k`` is a format error even though face repeats
        ("f03s05s06h12c07e04m02a09", "duplicate_field", "skin"),  # duplicate wins over the missing accessory colour
        ("s05s06f03f04h12c07e04m02a09k03", "duplicate_field", "skin"),  # the first repeat in reading order
        ("k03a09m02", "missing_field", "face"),  # the first missing one in FIELDS order, not in reading order
    ]
    for text, reason, field in cases:
        with pytest.raises(av.AvatarCodeError) as exc:
            av.decode_code(text)
        assert (exc.value.reason, exc.value.field) == (reason, field), text


def test_the_ignored_characters_are_an_explicit_list_not_the_white_space_class_of_either_language():
    """Python's ``\\s`` has U+001C–U+001F and JS's has U+FEFF: with ``\\s`` the same input would decode in one and fail in the
    other (found in review).  The list is spelled out, here and in ``static/js/avatar.js``; the shared vectors pin the cases."""
    assert av.CODE_IGNORED_CHARS == " \t\n\x0b\x0c\r\x85\u2028\u2029\ufeff-_:"
    base = "f03s05h12c07e04m02a09k03"
    ignored = [*av.CODE_IGNORED_CHARS, "\u00a0", "\u3000", "\u2000", "\u2003", "\u2009", "\u200a", "\u202f", "\u205f", "\uff0d", "\uff3f", "\uff1a"]
    for ch in ignored:  # (the last ones are made into ignorable ones by NFKC: NBSP, the wide spaces, the full-width ``-`` ``_`` ``:``)
        for text in (ch + base, base[:3] + ch + base[3:], base + ch):
            assert av.decode_code(text)["face"] == "f03", ascii(text)
    not_ignored = ["\x1c", "\x1d", "\x1e", "\x1f", "\u200b", "\u200c", "\u180e", "\u00ad", "\u2010", "\u2011", "\u2013", "\u2212", "\u1680", "\u0000", "\x7f"]
    for ch in not_ignored:
        for text in (ch + base, base[:3] + ch + base[3:], base + ch):
            with pytest.raises(av.AvatarCodeError) as exc:
                av.decode_code(text)
            assert exc.value.reason == "bad_format", ascii(text)
    for only in ("\ufeff", "\u0085", "\u2028\u2029", " \t\x0b\x0c\r\n", "\u3000\u00a0", "-_:"):
        with pytest.raises(av.AvatarCodeError) as exc:
            av.decode_code(only)
        assert exc.value.reason == "empty", ascii(only)


@pytest.mark.parametrize("value", [None, ""])
def test_decode_of_nothing_is_empty(value):
    with pytest.raises(av.AvatarCodeError) as exc:
        av.decode_code(value)
    assert exc.value.reason == "empty" and "头像编号" in str(exc.value)


@pytest.mark.parametrize("value", [5, 1.5, True, [], {}, b"f03s05h12c07e04m02a09k03", ("f03",)])
def test_decode_of_a_non_string_is_bad_format(value):
    with pytest.raises(av.AvatarCodeError) as exc:
        av.decode_code(value)  # type: ignore[arg-type]
    assert exc.value.reason == "bad_format" and CJK.search(str(exc.value))


def test_messages_name_the_offending_part_in_chinese():
    with pytest.raises(av.AvatarCodeError) as exc:
        av.decode_code("f03s05h12c07e04m02a09")
    assert exc.value.field == "accessory_color" and "配饰颜色" in str(exc.value) and "k" in str(exc.value)
    with pytest.raises(av.AvatarCodeError) as exc:
        av.decode_code("f03f04s05h12c07e04m02a09k03")
    assert exc.value.field == "face" and "脸型" in str(exc.value) and "不止一次" in str(exc.value)
    with pytest.raises(av.AvatarCodeError) as exc:
        av.decode_code("f03s05h12c07e04m02a09k03!")
    assert "'!'" in str(exc.value)  # which character is wrong
    with pytest.raises(av.AvatarCodeError) as exc:
        av.decode_code("x03s05h12c07e04m02a09k03")
    assert "x03" in str(exc.value)  # which segment is wrong


# ------------------------------------------------------------------------------------------------ against the real library
def test_unknown_parts_are_only_reported_when_a_library_is_given(library):
    code = "f99s05h12c07e04m02a09k03"
    assert av.decode_code(code)["face"] == "f99"  # format only: nothing to compare with
    with pytest.raises(av.AvatarCodeError) as exc:
        av.decode_code(code, library=library)
    err = exc.value
    assert (err.reason, err.field, err.part) == ("unknown_part", "face", "f99") and "脸型" in str(err) and "f99" in str(err)
    # the first unknown one in FIELDS order
    with pytest.raises(av.AvatarCodeError) as exc:
        av.decode_code("f03s05h12c07e04m02a99k99", library=library)
    assert (exc.value.field, exc.value.part) == ("accessory", "a99")
    # a colour the library does not have is unknown even when there is no accessory (validate_description agrees)
    with pytest.raises(av.AvatarCodeError) as exc:
        av.decode_code("f03s05h12c07e04m02a00k99", library=library)
    assert (exc.value.field, exc.value.part) == ("accessory_color", "k99")
    # structural problems are reported before unknown parts
    with pytest.raises(av.AvatarCodeError) as exc:
        av.decode_code("f99s05h12c07e04m02a09", library=library)
    assert exc.value.reason == "missing_field"


def test_every_part_in_the_library_has_a_code_that_decodes_back(library):
    ids = av.part_ids(library)
    for field in FIELDS:
        assert all(i[0] == av.FIELD_LETTERS[field] for i in ids[field]), field  # the letter convention the code relies on
    for field in FIELDS:
        for pid in ids[field]:
            desc = {**GOOD, field: pid}
            code = av.encode_description(desc)
            assert av.decode_code(code, library=library) == desc
            assert av.validate_description(av.decode_code(code, library=library), library) == desc


def test_random_avatars_round_trip_and_a_decoded_code_is_accepted_by_validate_description(library):
    seen = set()
    for seed in range(120):
        desc = av.random_description(library, av._rng_for(seed, "code"))
        code = av.encode_description(desc)
        assert CANONICAL.fullmatch(code) and code == code.lower() and "".join(desc[f] for f in FIELDS) == code
        assert av.decode_code(code, library=library) == av.validate_description(desc, library) == desc
        seen.add(code)
    assert len(seen) > 100  # different avatars get different codes
    # the same code always draws the same picture
    first = av.decode_code(GOOD_CODE, library=library)
    assert av.render_pixels(first, library) == av.render_pixels(av.decode_code(GOOD_CODE.upper(), library=library), library)


def test_a_whole_roster_round_trips(library):
    roster = av.make_roster(13, seed=7, parts=library)
    codes = [av.encode_description(d) for d in roster]
    assert len(set(codes)) == 13
    assert [av.decode_code(c, library=library) for c in codes] == roster


# ------------------------------------------------------------------------------------------------ encode
def test_encode_maps_none_to_a00_and_an_empty_accessory_colour_to_k00():
    assert av.encode_description({**GOOD, "accessory": "none"}) == "f03s05h12c07e04m02a00k03"
    assert av.encode_description({**GOOD, "accessory": "none", "accessory_color": ""}) == "f03s05h12c07e04m02a00k00"
    assert av.encode_description({**GOOD, "accessory": "a00", "accessory_color": ""}) == "f03s05h12c07e04m02a00k00"
    no_color = {k: v for k, v in GOOD.items() if k != "accessory_color"}
    assert av.encode_description({**no_color, "accessory": "a00"}) == "f03s05h12c07e04m02a00k00"
    # the encoded code decodes to the same picture (validate_description allows the empty colour only without an accessory)
    assert av.decode_code("f03s05h12c07e04m02a00k00")["accessory_color"] == "k00"
    # ... but with an accessory the colour is required
    with pytest.raises(av.AvatarCodeError) as exc:
        av.encode_description({**GOOD, "accessory_color": ""})
    assert (exc.value.reason, exc.value.field) == ("bad_format", "accessory_color")
    with pytest.raises(av.AvatarCodeError) as exc:
        av.encode_description(no_color)
    assert (exc.value.reason, exc.value.field) == ("missing_field", "accessory_color")


def test_encode_ignores_the_version_and_extra_keys_and_does_not_touch_its_input():
    desc = {**GOOD, "v": 7, "note": "x"}
    before = dict(desc)
    assert av.encode_description(desc) == GOOD_CODE and desc == before
    assert av.encode_description({k: v for k, v in GOOD.items() if k != "v"}) == GOOD_CODE


@pytest.mark.parametrize("field", FIELDS)
def test_encode_rejects_a_missing_field_and_a_malformed_or_misplaced_id(field):
    missing = {k: v for k, v in GOOD.items() if k != field}
    with pytest.raises(av.AvatarCodeError) as exc:
        av.encode_description(missing)
    assert (exc.value.reason, exc.value.field) == ("missing_field", field) and field in str(exc.value)
    wrong_letter = "z05" if av.FIELD_LETTERS[field] != "z" else "y05"
    for bad in ("", "05", "x", wrong_letter, GOOD[field].upper(), GOOD[field] + "\n", GOOD[field][0] + "0003", 3, None, ["f03"]):
        if bad is None:
            continue  # None counts as missing, covered above
        with pytest.raises(av.AvatarCodeError) as exc:
            av.encode_description({**GOOD, field: bad})
        assert (exc.value.reason, exc.value.field) == ("bad_format", field), (field, bad)
        assert CJK.search(str(exc.value))


def test_encode_rejects_an_id_that_belongs_to_another_field():
    # ``s05`` as a face would be read back as the skin: the round trip would silently change the avatar
    with pytest.raises(av.AvatarCodeError) as exc:
        av.encode_description({**GOOD, "face": "s05"})
    assert exc.value.reason == "bad_format" and exc.value.field == "face"


@pytest.mark.parametrize("bad", [None, [], "f03", 3, ("v", 1)])
def test_encode_rejects_a_non_dict(bad):
    with pytest.raises(av.AvatarCodeError) as exc:
        av.encode_description(bad)
    assert exc.value.reason == "bad_format" and "对象" in str(exc.value)


def test_the_error_class_is_a_value_error_with_the_documented_attributes():
    err = av.AvatarCodeError("unknown_part", "素材库里没有脸型 f99", field="face", part="f99")
    assert isinstance(err, ValueError) and (err.reason, err.field, err.part, str(err)) == ("unknown_part", "face", "f99", "素材库里没有脸型 f99")
    assert av.CODE_REASONS == ("empty", "bad_format", "missing_field", "duplicate_field", "unknown_part")
    assert {"encode_description", "decode_code", "AvatarCodeError", "FIELD_LETTERS", "CODE_REASONS"} <= set(av.__all__)
    assert av.FIELD_LETTERS == {"face": "f", "skin": "s", "hair": "h", "hair_color": "c", "eyes": "e", "mouth": "m", "accessory": "a", "accessory_color": "k"}
