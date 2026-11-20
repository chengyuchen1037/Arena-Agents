"""``app.storage.contract.ContractStore``: the official contract in the data directory (contract §13.2, H7, H38).

The first run copies the default, a default that was never touched follows the program, what the user imported (or edited by hand)
is theirs, every bad text is refused with every problem and writes nothing, the hash is the one the bots are stamped with."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

from app.agents.contract import load_default_contract, required_terms, validate_contract
from app.bots.types import CONTRACT_MAX_CHARS, CONTRACT_MUST_READ_CHARS, sha256_text
from app.storage import contract as contract_mod
from app.storage.contract import ContractError, ContractStore, get_contract_store

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = (ROOT / "prompts" / "contract.md").read_text(encoding="utf-8")


def drop_word(text: str, word: str, replacement: str = "xx") -> str:
    """``text`` with every mention of ``word`` (as a word: ``speak`` is not ``speak_template``) replaced."""
    return re.sub(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", replacement, text)


class Clock:
    def __init__(self, start: int = 1_763_600_000) -> None:
        self.now = start

    def __call__(self) -> float:
        return float(self.now)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def default_file(tmp_path: Path) -> Path:
    path = tmp_path / "default-contract.md"
    path.write_text(SHIPPED, encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def store(tmp_path: Path, default_file: Path, clock: Clock) -> ContractStore:
    contract_store = ContractStore(tmp_path / "data", default_path=default_file, clock=clock)
    contract_store.ensure_installed()
    return contract_store


def problems_of(contract_store: ContractStore, text) -> list[str]:
    with pytest.raises(ContractError) as info:
        contract_store.set(text)
    return info.value.problems


# ======================================================================================================================
# the shipped files
# ======================================================================================================================
@pytest.mark.parametrize("name", ["prompts/contract.md", "scenarios/werewolf_12p.md", "scenarios/minimal_test.md"])
def test_the_shipped_files_are_in_the_form_the_store_keeps_them(name: str):
    """LF line ends, no byte-order mark, exactly one final newline: then the data directory's copy and the file itself have the same hash,
    which is what the hash of a default bot's initialization is made of."""
    raw = (ROOT / name).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    assert raw.decode("utf-8") == contract_mod.normalize_text(raw.decode("utf-8"))


def test_the_shipped_contract_passes_its_own_check_and_is_the_default_document():
    assert validate_contract(SHIPPED) == []
    doc = load_default_contract()
    assert doc.markdown == SHIPPED and doc.version == "v0.14.0" and doc.sha256 == sha256_text(SHIPPED)
    assert len(doc.must_read) <= CONTRACT_MUST_READ_CHARS and len(SHIPPED) <= CONTRACT_MAX_CHARS


# ======================================================================================================================
# the first run
# ======================================================================================================================
def test_the_first_run_copies_the_default_contract_into_the_data_directory(tmp_path: Path, default_file: Path, clock: Clock):
    data = tmp_path / "data"
    assert not data.exists()
    store = ContractStore(data, default_path=default_file, clock=clock)
    assert not data.exists()  # (building a store creates nothing)
    store.ensure_installed()
    assert (data / "contract" / "contract.md").read_bytes() == SHIPPED.encode("utf-8")
    meta = json.loads((data / "contract" / "source.json").read_text(encoding="utf-8"))
    assert meta == {"origin": "default", "version": "v0.14.0", "imported_at": None, "sha256": sha256_text(SHIPPED)}
    assert store.source() == meta
    doc = store.current()
    assert doc.markdown == SHIPPED and doc.version == "v0.14.0" and doc.sha256 == meta["sha256"]
    assert doc.must_read and doc.reference and store.must_read_chars() == len(doc.must_read) <= CONTRACT_MUST_READ_CHARS
    assert store.validate(doc.markdown) == []
    # a second start changes nothing, not even the files' times
    before = {p.name: p.stat().st_mtime_ns for p in (data / "contract").iterdir()}
    ContractStore(data, default_path=default_file, clock=clock).ensure_installed()
    assert {p.name: p.stat().st_mtime_ns for p in (data / "contract").iterdir()} == before


def test_the_real_default_is_installed_when_no_path_is_given(isolated_external_storage: Path):
    store = get_contract_store()
    assert store.current().sha256 == load_default_contract().sha256  # (what the bots of the default library were stamped with)
    assert store.source()["origin"] == "default" and store.default_path().name == "contract.md"
    assert (isolated_external_storage / "contract" / "contract.md").is_file()


def test_the_process_wide_store_follows_the_data_directory(isolated_external_storage: Path, monkeypatch, tmp_path: Path):
    from app.storage import clear_store_caches

    first = get_contract_store()
    assert first is get_contract_store()
    other = tmp_path / "other-data"
    monkeypatch.setenv("ARENA_DATA_DIR", str(other))
    second = get_contract_store()  # (no reset needed: the directory changed)
    assert second is not first and (other / "contract" / "contract.md").is_file()
    clear_store_caches()
    assert get_contract_store() is not second  # (what the fixtures of the tests use to forget a store)


def test_a_missing_default_file_is_logged_and_leaves_no_contract(tmp_path: Path, caplog):
    store = ContractStore(tmp_path / "data", default_path=tmp_path / "nothing.md")
    with caplog.at_level("WARNING"):
        store.ensure_installed()
    assert "default contract file cannot be read" in caplog.text
    assert store.current().markdown == "" and store.current().must_read == ""
    assert store.source()["sha256"] == sha256_text("")
    with pytest.raises(FileNotFoundError):
        store.default()
    with pytest.raises(ContractError) as info:
        store.reset()
    assert info.value.status == 503 and info.value.code == "default_missing" and "找不到默认契约文件" in str(info.value)
    assert store.set(SHIPPED)["origin"] == "imported"  # (it can still be imported by hand: the default file is not the same text as the imported one here)


# ======================================================================================================================
# a text the user hands in
# ======================================================================================================================
def test_an_imported_contract_is_stored_normalized_and_is_the_users(store: ContractStore, tmp_path: Path, clock: Clock):
    custom = SHIPPED.replace("version: v0.14.0", "version: v9.9.1").replace("# ", "# ", 1)
    messy = "\ufeff" + custom.replace("\n", "\r\n") + "\r\n\r\n   \r\n"
    clock.now = 1_763_700_000
    source = store.set(messy)
    stored = (store.data_dir / "contract" / "contract.md").read_bytes()
    assert stored == custom.encode("utf-8")  # (BOM gone, LF, exactly one final newline)
    assert source == {"origin": "imported", "version": "v9.9.1", "imported_at": 1_763_700_000, "sha256": sha256_text(custom)}
    assert store.current().markdown == custom and store.current().version == "v9.9.1" and store.current().sha256 == source["sha256"]
    assert json.loads((store.data_dir / "contract" / "source.json").read_text(encoding="utf-8")) == source
    # putting back exactly what the program ships makes it the default again (by content), without a time
    back = store.set(SHIPPED)
    assert back["origin"] == "default" and back["imported_at"] is None and back["version"] == "v0.14.0"


def test_a_contract_without_a_version_is_custom(store: ContractStore, clock: Clock):
    custom = SHIPPED.replace("version: v0.14.0\n", "")
    assert validate_contract(custom) == []
    source = store.set(custom)
    assert source["version"] == "custom" and source["origin"] == "imported"
    assert store.current().version == "custom"


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda t: "", "契约是空的"),
        (lambda t: "   \n\n", "契约是空的"),
        (lambda t: t.split("---\n", 2)[2], "缺少格式头"),
        (lambda t: t.replace("format: arena-contract", "format: markdown"), "format 必须是 arena-contract（现在是 markdown）"),
        (lambda t: t.replace("version: v0.14.0", "version: a b/c"), "version 只能用字母"),
        (lambda t: t.replace("## 必读", "## 必须读"), "缺少“## 必读”一节"),
        (lambda t: t.replace("## 参考", "## 参考资料"), "缺少“## 参考”一节"),
        (lambda t: t + "\n## 必读\n又一节\n", "“## 必读”出现了 2 次"),
        (lambda t: t.replace("## 必读\n", "## 必读\n" + "这是一段很长的话。" * 140 + "\n\n", 1), "“必读”最多 1200 字"),
        (lambda t: drop_word(t, "jot", "jxt"), "契约没有提到这些工具：jot"),
        (lambda t: drop_word(t, "TOO_LONG", "TOO_LNG"), "契约没有提到这些回执码：TOO_LONG"),
        (lambda t: t + "\n" + "补" * CONTRACT_MAX_CHARS, f"契约最多 {CONTRACT_MAX_CHARS} 字"),
        (lambda t: t + "\x00", "含有空字符"),
        (lambda t: t + "\ud800", "无法保存的字符"),
    ],
)
def test_a_contract_that_cannot_be_used_is_refused_in_chinese_and_nothing_is_written(store: ContractStore, mutate, fragment):
    before = (store.path.read_bytes(), (store.data_dir / "contract" / "source.json").read_bytes())
    problems = problems_of(store, mutate(SHIPPED))
    assert any(fragment in problem for problem in problems), problems
    assert (store.path.read_bytes(), (store.data_dir / "contract" / "source.json").read_bytes()) == before
    assert store.current().markdown == SHIPPED


def test_every_problem_is_listed_together_with_its_numbers(store: ContractStore):
    text = drop_word(drop_word(SHIPPED.replace("format: arena-contract", "format: other"), "speak_template", "xx"), "NO_FLOOR", "NO_FLR")
    problems = problems_of(store, text)
    assert len(problems) == 3
    assert "契约没有提到这些工具：speak_template" in " ".join(problems) and "契约没有提到这些回执码：NO_FLOOR" in " ".join(problems)
    with pytest.raises(ContractError) as info:
        store.set(text)
    assert str(info.value) == " ".join(problems) and info.value.status == 400 and info.value.code == "invalid"
    assert store.validate(text) == problems  # (validate says the same without raising)
    assert store.validate(SHIPPED) == []
    assert store.validate(None) == ["契约是空的。"] and store.validate(5) == ["契约必须是文字。"]  # type: ignore[arg-type]


def test_every_tool_and_every_receipt_code_has_to_be_mentioned(store: ContractStore):
    """The terms come from ``required_terms()`` (mind): a contract that drops any one of them is refused naming it."""
    terms = required_terms()
    assert "speak" in terms and "TOO_LONG" in terms and "jot" in terms and len(terms) > 30
    for term in terms:
        problems = store.validate(drop_word(SHIPPED, term))
        assert problems and any(term in problem for problem in problems), term


def test_a_refused_import_leaves_an_imported_contract_as_it_was(store: ContractStore, clock: Clock):
    clock.now = 1_763_800_000
    custom = SHIPPED.replace("version: v0.14.0", "version: v1.0.0")
    store.set(custom)
    problems_of(store, "随便写点什么")
    assert store.current().markdown == custom and store.source()["imported_at"] == 1_763_800_000


def test_reset_brings_the_default_back(store: ContractStore, clock: Clock):
    store.set(SHIPPED.replace("version: v0.14.0", "version: v1.0.0"))
    assert store.source()["origin"] == "imported"
    source = store.reset()
    assert source == {"origin": "default", "version": "v0.14.0", "imported_at": None, "sha256": sha256_text(SHIPPED)}
    assert store.path.read_bytes() == SHIPPED.encode("utf-8")
    assert store.reset() == source  # (idempotent)


# ======================================================================================================================
# the program is upgraded, the user edits by hand
# ======================================================================================================================
def test_an_untouched_default_follows_the_program_when_it_ships_another_contract(tmp_path: Path, default_file: Path, clock: Clock):
    data = tmp_path / "data"
    ContractStore(data, default_path=default_file, clock=clock).ensure_installed()
    newer = SHIPPED.replace("version: v0.14.0", "version: v0.14.1")
    default_file.write_text(newer, encoding="utf-8", newline="\n")  # (a new version of the program)
    upgraded = ContractStore(data, default_path=default_file, clock=clock)
    upgraded.ensure_installed()
    assert upgraded.current().markdown == newer and upgraded.source() == {
        "origin": "default", "version": "v0.14.1", "imported_at": None, "sha256": sha256_text(newer),
    }


def test_what_the_user_imported_is_never_replaced_by_a_newer_default(tmp_path: Path, default_file: Path, clock: Clock):
    data = tmp_path / "data"
    first = ContractStore(data, default_path=default_file, clock=clock)
    first.ensure_installed()
    mine = SHIPPED.replace("version: v0.14.0", "version: mine")
    clock.now = 1_763_900_000
    first.set(mine)
    default_file.write_text(SHIPPED.replace("version: v0.14.0", "version: v0.14.1"), encoding="utf-8", newline="\n")
    again = ContractStore(data, default_path=default_file, clock=clock)
    again.ensure_installed()
    assert again.current().markdown == mine and again.source()["origin"] == "imported" and again.source()["imported_at"] == 1_763_900_000


def test_a_file_edited_by_hand_is_reported_as_the_users_and_kept(tmp_path: Path, default_file: Path, clock: Clock):
    data = tmp_path / "data"
    first = ContractStore(data, default_path=default_file, clock=clock)
    first.ensure_installed()
    edited = SHIPPED + "\n（手工加的一句话。）\n"
    (data / "contract" / "contract.md").write_text(edited, encoding="utf-8", newline="\n")  # (an editor, outside the program)
    source = first.source()
    assert source["origin"] == "imported" and source["sha256"] == sha256_text(edited) and isinstance(source["imported_at"], int)
    assert first.current().markdown == edited  # (nothing is cached: it is seen at once)
    default_file.write_text(SHIPPED.replace("version: v0.14.0", "version: v0.14.1"), encoding="utf-8", newline="\n")
    restarted = ContractStore(data, default_path=default_file, clock=clock)
    restarted.ensure_installed()
    assert restarted.current().markdown == edited  # (it is not an untouched default: it stays)
    # editing it back to the shipped text makes it the default again, whatever source.json said
    default_file.write_text(SHIPPED, encoding="utf-8", newline="\n")
    (data / "contract" / "contract.md").write_text(SHIPPED, encoding="utf-8", newline="\n")
    assert restarted.source()["origin"] == "default"


def test_a_lost_source_file_is_rebuilt_from_the_contract_itself(store: ContractStore):
    (store.data_dir / "contract" / "source.json").unlink()
    assert store.source()["origin"] == "default"  # (the text is the shipped one)
    store.set(SHIPPED.replace("version: v0.14.0", "version: v7"))
    (store.data_dir / "contract" / "source.json").write_text("not json", encoding="utf-8")
    assert store.source()["origin"] == "imported" and store.source()["version"] == "v7"
    (store.data_dir / "contract" / "source.json").write_text(json.dumps({"origin": "default", "sha256": "0" * 64}), encoding="utf-8")
    assert store.source()["origin"] == "imported"  # (a source that is not about this text is not believed)


def test_a_deleted_contract_is_empty_until_the_next_start_or_a_reset(tmp_path: Path, default_file: Path, clock: Clock):
    data = tmp_path / "data"
    store = ContractStore(data, default_path=default_file, clock=clock)
    store.ensure_installed()
    (data / "contract" / "contract.md").unlink()
    assert store.current().markdown == "" and store.current().must_read == ""
    assert store.source()["origin"] == "imported" and store.source()["sha256"] == sha256_text("")
    store.reset()
    assert store.current().markdown == SHIPPED
    (data / "contract" / "contract.md").unlink()
    restarted = ContractStore(data, default_path=default_file, clock=clock)
    restarted.ensure_installed()
    assert restarted.current().markdown == SHIPPED and restarted.source()["origin"] == "default"
    (data / "contract" / "contract.md").write_text("   \n", encoding="utf-8")  # (blank counts as missing)
    ContractStore(data, default_path=default_file, clock=clock).ensure_installed()
    assert (data / "contract" / "contract.md").read_text(encoding="utf-8") == SHIPPED


def test_a_symbolic_link_is_not_followed(tmp_path: Path, default_file: Path, clock: Clock):
    data = tmp_path / "data"
    store = ContractStore(data, default_path=default_file, clock=clock)
    store.ensure_installed()
    outside = tmp_path / "outside.md"
    outside.write_text("秘密内容", encoding="utf-8")
    (data / "contract" / "contract.md").unlink()
    try:
        (data / "contract" / "contract.md").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symbolic links here")
    assert store.current().markdown == ""  # (it reads as missing, not as the file it points to)


# ======================================================================================================================
# the download name (H42)
# ======================================================================================================================
def test_the_download_label_is_the_version_or_custom_and_a_date(store: ContractStore, clock: Clock):
    assert store.download_label() == "v0.14.0"
    store.set(SHIPPED.replace("version: v0.14.0", "version: v2.0.0-rc1"))
    assert store.download_label() == "v2.0.0-rc1"
    clock.now = int(time.mktime((2026, 11, 20, 12, 0, 0, 0, 0, -1)))
    store.set(SHIPPED.replace("version: v0.14.0\n", ""))
    assert store.download_label() == "custom-20261120"
    clock.now += 86400 * 3
    assert store.download_label() == "custom-20261120"  # (the day it was imported, not today)
    (store.data_dir / "contract" / "source.json").unlink()
    assert re.fullmatch(r"custom-\d{8}", store.download_label())


# ======================================================================================================================
# concurrency
# ======================================================================================================================
def test_a_reader_never_sees_a_half_written_contract(store: ContractStore):
    a = SHIPPED.replace("version: v0.14.0", "version: va")
    b = SHIPPED.replace("version: v0.14.0", "version: vb")
    store.set(a)
    seen: set[str] = set()
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                doc = store.current()
                seen.add(doc.markdown)
                assert doc.sha256 in (sha256_text(a), sha256_text(b))
                store.source()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(30):
            store.set(b if index % 2 == 0 else a)
    finally:
        stop.set()
        thread.join(timeout=20)
    assert not errors, errors
    assert seen <= {a, b} and seen
    assert sorted(p.name for p in (store.data_dir / "contract").iterdir()) == ["contract.md", "source.json"]  # (no temporary file left)
