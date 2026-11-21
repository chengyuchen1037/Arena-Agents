"""``python -m bench situations | run | summarize | replay``: the commands, end to end, on the fake model (the real-model mode is exercised with a stub for
the transport only).  Every output goes to pytest's ``tmp_path``; no test makes a directory anywhere else."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from bench_helpers import play_long_game

from app.llm.types import LLMResponse, ToolCallData
from bench import cli, safety
from bench.__main__ import main
from bench.realllm import DEFAULT_KEY_ENV, build_real_port
from bench.replay import decision_points
from bench.situation import load_situation

ROOT = Path(__file__).resolve().parents[2]
KEY = "sk-test-0123456789abcdef"
EXAMPLES = ROOT / "bench" / "fixtures" / "situations"
#: The two plain examples, named outright - not the directory: what is committed next to them changes, these tests do not.
SITS = ["--situations", str(EXAMPLES / "_example.yaml"), "--situations", str(EXAMPLES / "_example_speak.yaml")]
BASE = [*SITS, "--bots", "legacy,bot:Alice"]


@pytest.fixture(autouse=True)
def no_ambient_model(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (DEFAULT_KEY_ENV, cli.BASE_URL_ENV, cli.MODEL_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_system_temp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A benchmark command makes nothing in the system's temporary directory (in some sandboxes that is the repository root)."""

    def boom(*args: object, **kwargs: object) -> str:
        raise AssertionError("tempfile must not be used by a benchmark command")

    monkeypatch.setattr(tempfile, "mkdtemp", boom)
    monkeypatch.setattr(tempfile, "mkstemp", boom)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", boom)


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out + out.err


def rows_in(out: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()]


def files_in(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


# ------------------------------------------------------------------------------------------------ situations


def test_the_situations_command_lists_and_checks(capsys: pytest.CaptureFixture[str]) -> None:
    code, text = run(["situations"], capsys)
    assert code == 0 and "example-vote" not in text  # (an example is not run, nor listed, unless asked for)
    code, text = run(["situations", "--include-examples"], capsys)
    assert code == 0 and "example-vote  [example, L1]  minimal_test  座位 N3  步骤 11" in text and "example-speak" in text and "example-pinned" in text and "✔" not in text
    assert "example-pinned  [example, M3]  minimal_test  座位 N3  步骤 3  示例：规则小测，自带一个有记忆的 bot  自带 bot：N3=bot:Zed" in text
    code, text = run(["situations", *SITS, "--check"], capsys)
    assert code == 0 and text.count("✔ N3：") == 2 and "叫醒它的是 request.opened" in text and "叫醒它的是 floor.granted" in text
    code, text = run(["situations", "--situations", str(EXAMPLES / "_example_pinned.yaml"), "--check"], capsys)
    assert code == 0 and "✔ N3：" in text and "叫醒它的是 request.opened" in text


def test_a_broken_situation_file_is_named_and_the_check_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "bad.yaml").write_text("format: arena-situation\nversion: 1\nid: bad\nscenario: minimal_test\ndecision: {seat: N3}\nsteps: oops\n", encoding="utf-8")
    code, text = run(["situations", "--situations", str(tmp_path)], capsys)
    assert code == 1 and "题目文件有问题" in text and "bad.yaml" in text and "steps 必须是一个列表" in text
    stuck = tmp_path / "stuck.yaml"
    stuck.write_text(
        "format: arena-situation\nversion: 1\nid: stuck\nscenario: minimal_test\nplayers: {count: 4}\nroles: [red, red, blue, blue]\ndecision: {seat: N3}\n"
        "steps:\n  - {as: moderator, tool: announce, args: {channel_id: nowhere, template_id: x}}\n", encoding="utf-8")
    code, text = run(["situations", "--situations", str(stuck), "--check"], capsys)
    assert code == 1 and "✖ N3" in text and "UNKNOWN_CHANNEL" in text


# ------------------------------------------------------------------------------------------------ run


def test_a_run_writes_its_files_and_prints_the_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    code, text = run(["run", *BASE, "--samples", "2", "--fake", "mixed", "--out", str(out), "--run-id", "r1"], capsys)
    assert code == 0 and files_in(out) == ["judge_items.jsonl", "results.jsonl", "run.json", "summary.json"]  # (no scratch directory left behind)
    assert "运行 r1：2 道题 × 2 个 bot × 2 次 = 8 个样本；模式 fake（假模型 mixed，不调用任何模型）" in text and "bot | 样本 | 有决定 | 一次过" in text
    rows = rows_in(out)
    assert len(rows) == 8 and {r["run"] for r in rows} == {"r1"} and {(r["mode"], r["model"]) for r in rows} == {("fake", "fake")}
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert (info["format"], info["mode"], info["model"], info["real"], info["rows"], info["model_calls"]) == ("arena-bench-run", "fake", "fake", None, 8, 10) or info["rows"] == 8
    assert [b["label"] for b in info["bots"]] == ["legacy", "bot:Alice"] and info["config"]["samples"] == 2
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["overall"]["rows"] == 8 and set(summary["bots"]) == {"legacy", "bot:Alice"}
    items = [json.loads(line) for line in (out / "judge_items.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(items) == 6 and {i["rubric"] for i in items} == {"L1", "S2"}  # (4 L1 + the 2 S2 of bot:Alice: legacy has no voice)


def test_without_out_the_results_go_to_the_scratch_directory_under_the_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(safety, "DEFAULT_RUNS_DIR", tmp_path / "runs")
    code, text = run(["run", *BASE, "--run-id", "fixed-id"], capsys)
    assert code == 0 and (tmp_path / "runs" / "fixed-id" / "results.jsonl").is_file() and f"结果写到 {tmp_path / 'runs' / 'fixed-id'}" in text
    assert files_in(tmp_path / "runs") == ["fixed-id"]
    code, text = run(["run", *BASE], capsys)  # (a generated id: timestamp and four hex digits)
    assert code == 0 and len(files_in(tmp_path / "runs")) == 2


def test_the_default_scratch_directory_is_inside_the_projects_ignored_directory() -> None:
    assert safety.default_out_dir("x") == ROOT / ".pytest_tmp" / "bench-runs" / "x"
    gitignore = ROOT / ".gitignore"
    if gitignore.is_file():  # (a fresh checkout - CI - has none: the project does not track it)
        assert ".pytest_tmp/" in gitignore.read_text(encoding="utf-8").replace("\\", "/")


def test_while_a_run_goes_on_the_data_directory_is_a_scratch_directory_inside_the_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    users = tmp_path / "users-data"
    monkeypatch.setenv("ARENA_DATA_DIR", str(users))
    seen: dict[str, Any] = {}
    original = cli.run_suite

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen["dir"] = Path(os.environ["ARENA_DATA_DIR"])
        seen["exists"] = seen["dir"].is_dir()
        return await original(*args, **kwargs)

    monkeypatch.setattr(cli, "run_suite", spy)
    out = tmp_path / "out"
    code, _ = run(["run", *BASE, "--out", str(out)], capsys)
    assert code == 0 and seen["exists"] and seen["dir"].parent == out and seen["dir"].name.startswith(".arena-data-")
    assert os.environ["ARENA_DATA_DIR"] == str(users) and not seen["dir"].exists() and not users.exists()  # (restored; the user's directory never made, never opened)


def test_ids_tags_and_bots_choose_what_is_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    code, _ = run(["run", *SITS, "--ids", "example-vote", "--bots", "legacy", "--out", str(out)], capsys)
    assert code == 0 and [(r["situation"], r["bot"]) for r in rows_in(out)] == [("example-vote", "legacy")]
    out2 = tmp_path / "out2"
    code, _ = run(["run", *SITS, "--tags", "S2", "--bots", "legacy", "--bots", "bot:Alice", "--out", str(out2)], capsys)
    assert code == 0 and [(r["situation"], r["bot"]) for r in rows_in(out2)] == [("example-speak", "legacy"), ("example-speak", "bot:Alice")]
    out3 = tmp_path / "out3"
    code, _ = run(["run", "--situations", str(ROOT / "bench" / "fixtures" / "situations" / "_example_speak.yaml"), "--bots", "bot:Alice@nomem,no-people", "--out", str(out3)], capsys)
    assert code == 0 and [r["bot"] for r in rows_in(out3)] == ["bot:Alice@nomem,no-people"]


def test_a_situation_that_brings_its_own_bot_needs_no_bots_argument_and_is_played_by_that_bot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pinned = str(EXAMPLES / "_example_pinned.yaml")
    out = tmp_path / "out"
    code, text = run(["run", "--situations", pinned, "--samples", "2", "--out", str(out)], capsys)  # (every decision seat is pinned: no default bots are made)
    assert code == 0 and "1 个座位由题目自带的 bot 来答（样本数按它们算）" in text and "= 2 个样本" in text
    rows = rows_in(out)
    assert [(r["bot"], r["name"], r["sample"], r["status"], r["correct"]) for r in rows] == [("bot:Zed", "Zed", 0, "ok", True), ("bot:Zed", "Zed", 1, "ok", True)]
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert [(b["label"], b["brain"], b["playbook_entries"], b["people"]) for b in info["bots"]] == [("bot:Zed", "BotBrain", 1, 1)]
    assert "规则里没写的事" not in json.dumps(info, ensure_ascii=False)  # (the memory text is the bot's own: a run records its size, not its words)
    # next to a situation without a bot of its own, the bots of --bots play that one and the pinned seat is still Zed's alone
    out2 = tmp_path / "out2"
    code, text = run(["run", "--situations", pinned, "--situations", str(EXAMPLES / "_example_speak.yaml"), "--bots", "legacy,bot:Alice", "--out", str(out2)], capsys)
    assert code == 0 and [(r["situation"], r["bot"]) for r in rows_in(out2)] == [("example-pinned", "bot:Zed"), ("example-speak", "legacy"), ("example-speak", "bot:Alice")]
    items = [json.loads(line) for line in (out2 / "judge_items.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {(i["rubric"], i["bot"]) for i in items} == {("M3", "bot:Zed"), ("S2", "bot:Alice")}  # (legacy has no voice: nothing for a judge to recognise)
    s2 = next(i for i in items if i["rubric"] == "S2")
    assert sorted(s2["truth"]["cards"].values()) == ["bot:Alice", "bot:Zed"] and sorted(c["id"] for c in s2["context"]["cards"]) == ["A", "B"]  # (the judge's cards: the bots of the run that have a voice)
    assert "bot:" not in json.dumps(s2["context"], ensure_ascii=False)  # (letters, not names)


def test_a_bot_a_situation_brings_that_cannot_be_made_refuses_the_run_before_anything_is_made(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = yaml.safe_load((EXAMPLES / "_example_pinned.yaml").read_text(encoding="utf-8"))
    data["bots"] = {"N3": "bot:./minds/nobody"}  # (a directory that is not next to the file, and no such bot in the library)
    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    code, printed = run(["run", "--situations", str(broken), "--out", str(tmp_path / "out")], capsys)
    assert code == 2 and "bot 有问题：example-pinned（bots.N3）：找不到 bot './minds/nobody'" in printed and not (tmp_path / "out").exists()
    data["bots"] = {"N3": "bot:Alice@sparkles"}  # (a spec that is wrong in itself is refused when the file is read, naming the file)
    broken.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    code, printed = run(["run", "--situations", str(broken), "--out", str(tmp_path / "out")], capsys)
    assert code == 2 and "题目文件有问题：broken.yaml" in printed and "不认识的选项 sparkles" in printed and not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("argv", "text"),
    [
        (["--samples", "0"], "--samples 和 --max-rounds 必须至少是 1"),
        (["--max-rounds", "0"], "--samples 和 --max-rounds 必须至少是 1"),
        (["--max-calls", "-1"], "--max-calls 不能是负数"),
        (["--fake", "nope"], "不认识的假模型策略 'nope'"),
        (["--ids", "nope"], "找不到这些题目：nope"),
        (["--bots", "alice"], "bot 的种类只能是 legacy 或 bot"),
        (["--bots", "bot:Nobody"], "找不到 bot 'Nobody'"),
        (["--bots", "legacy,legacy"], "重复的 bot：legacy"),
    ],
)
def test_a_run_that_is_refused_is_refused_before_anything_is_made(argv: list[str], text: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    base = ["run", *SITS, "--bots", "legacy", "--out", str(out)]
    code, printed = run([*base, *argv], capsys)
    assert code == 2 and text in printed and not out.exists() and files_in(tmp_path) == []


def test_no_situations_to_run_is_a_refusal_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, text = run(["run", "--situations", str(tmp_path / "empty"), "--out", str(tmp_path / "o")], capsys)
    assert code == 2 and "不存在" in text
    (tmp_path / "dir").mkdir()
    code, text = run(["run", "--situations", str(tmp_path / "dir"), "--out", str(tmp_path / "o")], capsys)
    assert code == 2 and "没有要跑的题目" in text and not (tmp_path / "o").exists()


@pytest.mark.parametrize("sub", ["data", "data/bench-x", "data/games", ".demo-data", ".demo-data/real", "."])
def test_an_output_directory_that_is_or_contains_the_real_data_is_refused(sub: str, capsys: pytest.CaptureFixture[str]) -> None:
    target = (ROOT / sub).resolve()
    existed = target.exists()
    code, text = run(["run", *BASE, "--out", str(target)], capsys)
    assert code == 2 and "基准不能写进真实的数据目录" in text and target.exists() == existed  # (and nothing was created there)
    assert not (ROOT / "data" / "bench-x").exists()


def test_a_run_can_be_resumed_and_only_what_is_missing_is_done_again(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    assert run(["run", *BASE, "--out", str(out), "--run-id", "a"], capsys)[0] == 0
    lines = (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    (out / "results.jsonl").write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")  # (the last sample is lost: the run died)
    code, text = run(["run", *BASE, "--out", str(out), "--run-id", "a", "--resume"], capsys)
    assert code == 0 and text.count("：ok（decision") == 1 and "跳过 3" in text
    assert len(rows_in(out)) == 4 and sum(1 for r in rows_in(out) if r["status"] == "ok") == 4


def test_the_conversation_of_every_sample_can_be_kept(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    assert run(["run", *BASE, "--out", str(out), "--dump-prompts", "--fake", "listy"], capsys)[0] == 0
    assert files_in(out / "prompts") == ["example-speak__N3__bot-Alice__0.md", "example-speak__N3__legacy__0.md", "example-vote__N3__bot-Alice__0.md", "example-vote__N3__legacy__0.md"]
    text = (out / "prompts" / "example-speak__N3__bot-Alice__0.md").read_text(encoding="utf-8")
    assert text.startswith("## system") and '"code":"FORMAT"' in text
    assert not (tmp_path / "x").exists() and not list(out.glob("**/.arena-data-*"))


# ------------------------------------------------------------------------------------------------ the real-model mode is off unless asked for


def test_the_real_mode_is_off_by_default_and_a_default_run_never_builds_a_real_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a real model port was built without --real")

    monkeypatch.setattr(cli, "build_real_port", boom)
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)  # (even with a key in the environment and a model named there)
    monkeypatch.setenv(cli.MODEL_ENV, "some-model")
    monkeypatch.setenv(cli.BASE_URL_ENV, "https://api.example.test/v1")
    out = tmp_path / "out"
    code, text = run(["run", *BASE, "--out", str(out), "--max-calls", "100"], capsys)
    assert code == 0 and "模式 fake" in text and KEY not in text
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert info["mode"] == "fake" and info["model"] == "fake" and info["real"] is None and {r["mode"] for r in rows_in(out)} == {"fake"}


@pytest.mark.parametrize(
    ("argv", "env", "text"),
    [
        ([], {}, "真实模型模式必须同时给 --max-calls N"),
        (["--max-calls", "0"], {}, "真实模型模式必须同时给 --max-calls N"),
        (["--max-calls", "5"], {}, "需要 --base-url"),
        (["--max-calls", "5", "--base-url", "https://x.test/v1"], {}, "需要 --model"),
        (["--max-calls", "5", "--base-url", "https://x.test/v1", "--model", "m"], {}, f"环境变量 {DEFAULT_KEY_ENV} 里没有 API Key"),
        (["--max-calls", "5", "--base-url", "x.test", "--model", "m"], {}, "--base-url 要以 http:// 或 https:// 开头"),
        (["--max-calls", "5", "--base-url", "https://x.test/v1", "--model", "m", "--api-key-env", KEY], {}, "不是 Key 本身"),
        (["--max-calls", "5", "--base-url", "https://x.test/v1", "--model", "m", "--api-key-env", "OTHER"], {DEFAULT_KEY_ENV: KEY}, "环境变量 OTHER 里没有 API Key"),
        (["--max-calls", "5", "--model", "m"], {cli.BASE_URL_ENV: "x.test"}, "--base-url 要以 http:// 或 https:// 开头"),
    ],
)
def test_a_real_run_needs_every_flag_and_a_key_and_is_refused_before_anything_is_made(
    argv: list[str], env: dict[str, str], text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    out = tmp_path / "out"
    code, printed = run(["run", *BASE, "--real", *argv, "--out", str(out)], capsys)
    assert code == 2 and text in printed and "运行 " not in printed and not out.exists() and files_in(tmp_path) == []
    assert KEY not in printed or text == "不是 Key 本身" and False


def stubbed_builder(calls: list[dict[str, Any]], configs: list[Any] | None = None) -> Any:
    """``build_real_port`` as production has it (it reads the key from the environment and builds the engine's client) with the HTTP transport replaced
    by a stub that answers every call with ``respond(r1, N1)``.  ``configs`` collects the ``RealConfig`` the command made."""

    def build(config: Any, **kwargs: Any) -> Any:
        if configs is not None:
            configs.append(config)
        real = build_real_port(config, **kwargs)

        async def chat_stream(messages: list[dict[str, Any]], **kw: Any) -> LLMResponse:
            calls.append({"binding": kw.get("binding"), "tools": [t["function"]["name"] for t in kw.get("tools") or []]})
            call = ToolCallData(id=f"c{len(calls)}", name="respond", arguments_json=json.dumps({"request_id": "r1", "value": "N1"}))
            return LLMResponse(tool_calls=[call], prompt_tokens=100, completion_tokens=10, finish_reason="tool_calls", provider_id="bench", model_id=config.model)

        real.client.chat_stream = chat_stream
        return real

    return build


def test_a_real_run_with_a_stub_for_the_transport_is_counted_capped_and_leaks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "build_real_port", stubbed_builder(calls))
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    out = tmp_path / "out"
    argv = ["run", *SITS, "--ids", "example-vote", "--bots", "legacy,bot:Alice", "--samples", "3", "--real", "--max-calls", "4", "--model", "m-1",
            "--base-url", "https://me:hunter2@api.example.test/v1?token=zzz", "--price-in", "1", "--price-out", "4", "--out", str(out)]
    code, text = run(argv, capsys)
    assert code == 0 and "模式 real（m-1 @ api.example.test，调用上限 4，最坏情况 72 次）" in text
    assert len(calls) == 4 and all(c["binding"].model_id == "m-1" and "respond" in c["tools"] for c in calls)  # (4 calls were allowed, 4 were made)
    rows = rows_in(out)
    assert len(rows) == 4 and {(r["mode"], r["model"], r["status"]) for r in rows} == {("real", "m-1", "ok")} and all(r["correct"] is True for r in rows)
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert (info["mode"], info["model"], info["model_calls"], info["rows"], info["skipped"]) == ("real", "m-1", 4, 4, 2) and info["config"]["max_calls"] == 4 and info["config"]["concurrency"] == 4
    assert info["real"]["base_url"] == "https://api.example.test/v1" and info["real"]["api_key_env"] == DEFAULT_KEY_ENV and info["usage"]["prompt"] == 400
    assert info["cost_usd_upper_bound"] == 0.0006  # (400 × 1 + 40 × 4) / 1e6
    everything = text + "".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    assert KEY not in everything and "hunter2" not in everything and "token=zzz" not in everything


PROVIDERS = {
    "schema_version": 1,
    "providers": [{
        "id": "deep", "name": "DeepSeek", "protocol": "openai_chat", "base_url": "https://api.deepseek.test", "auto_append_path": False, "api_key": "sk-IN-THE-FILE-0123456789",
        "models": [{"id": "flash", "input_price_per_m": 0.27, "output_price_per_m": 1.1, "context_window_tokens": 128000}, {"id": "pro", "input_price_per_m": 1.0, "output_price_per_m": 4.0}],
    }],
}


def test_a_real_run_can_reuse_a_provider_from_a_file_the_user_names_and_a_flag_overrides_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[dict[str, Any]] = []
    configs: list[Any] = []
    monkeypatch.setattr(cli, "build_real_port", stubbed_builder(calls, configs))
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    providers = tmp_path / "providers.json"
    providers.write_text(json.dumps(PROVIDERS), encoding="utf-8")
    before = providers.read_bytes()
    base = ["run", *SITS, "--ids", "example-vote", "--bots", "legacy", "--real", "--max-calls", "1", "--providers-file", str(providers), "--provider", "deep"]
    out = tmp_path / "out"
    code, text = run([*base, "--out", str(out)], capsys)
    assert code == 0 and "模式 real（flash @ api.deepseek.test" in text
    cfg = configs[0]
    assert (cfg.base_url, cfg.model, cfg.protocol, cfg.context_window, cfg.input_price_per_m, cfg.output_price_per_m, cfg.auto_append_path, cfg.provider) == (
        "https://api.deepseek.test", "flash", "openai_chat", 128000, 0.27, 1.1, False, "deep")
    info = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert info["model"] == "flash" and info["real"]["provider"] == "deep" and info["real"]["context_window"] == 128000 and "cost_usd_upper_bound" in info
    everything = text + "".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file())
    assert KEY not in everything and "sk-IN-THE-FILE" not in everything and str(providers) not in everything  # (the key is the environment's; the file's own is never used)
    out2 = tmp_path / "out2"
    code, _ = run([*base, "--model", "pro", "--price-in", "9", "--protocol", "openai_responses", "--context-window", "5000", "--out", str(out2)], capsys)
    assert code == 0
    cfg = configs[1]  # (a flag wins over the file, the file over the defaults: the output price is still the file's)
    assert (cfg.model, cfg.protocol, cfg.context_window, cfg.input_price_per_m, cfg.output_price_per_m) == ("pro", "openai_responses", 5000, 9.0, 4.0)
    assert providers.read_bytes() == before  # (only read)


@pytest.mark.parametrize(
    ("argv", "text"),
    [
        (["--real", "--max-calls", "5", "--providers-file", "PROVIDERS"], "--providers-file 要同时给 --provider"),
        (["--real", "--max-calls", "5", "--provider", "deep"], "--provider 要和 --providers-file 一起用"),
        (["--real", "--max-calls", "5", "--providers-file", "MISSING", "--provider", "deep"], "读不了 --providers-file missing.json"),
        (["--real", "--max-calls", "5", "--providers-file", "PROVIDERS", "--provider", "nope"], "里没有 provider 'nope'。有：deep"),
        (["--real", "--max-calls", "5", "--providers-file", "PROVIDERS", "--provider", "deep", "--model", "slow"], "provider deep 里没有模型 'slow'。有：flash、pro"),
        (["--providers-file", "PROVIDERS", "--provider", "deep"], "只在 --real 时有用"),
    ],
)
def test_a_provider_file_that_cannot_be_used_refuses_the_run_before_anything_is_made(
    argv: list[str], text: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    providers = tmp_path / "providers.json"
    providers.write_text(json.dumps(PROVIDERS), encoding="utf-8")
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    out = tmp_path / "out"
    resolved = [str(providers) if a == "PROVIDERS" else str(tmp_path / "missing.json") if a == "MISSING" else a for a in argv]
    code, printed = run(["run", *SITS, "--bots", "legacy", *resolved, "--out", str(out)], capsys)
    assert code == 2 and text in printed and "运行 " not in printed and not out.exists() and KEY not in printed


def test_a_real_run_that_runs_out_of_budget_says_so_and_can_be_resumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "build_real_port", stubbed_builder(calls))
    monkeypatch.setenv(DEFAULT_KEY_ENV, KEY)
    out = tmp_path / "out"
    argv = ["run", *SITS, "--ids", "example-vote", "--bots", "legacy", "--samples", "5", "--real", "--max-calls", "2", "--model", "m",
            "--base-url", "http://localhost:1", "--out", str(out), "--run-id", "x"]
    code, text = run(argv, capsys)
    assert code == 0 and len(calls) == 2 and "跳过 3" in text and len(rows_in(out)) == 2
    code, text = run([*argv, "--resume"], capsys)  # (a second budget of two: the two done samples are skipped, two more are made)
    assert code == 0 and len(calls) == 4 and len(rows_in(out)) == 4


# ------------------------------------------------------------------------------------------------ summarize


def test_summarize_reads_a_results_file_and_can_write_the_numbers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    assert run(["run", *BASE, "--samples", "2", "--out", str(out)], capsys)[0] == 0
    code, text = run(["summarize", "--results", str(out / "results.jsonl"), "--out", str(tmp_path / "s" / "again.json")], capsys)
    assert code == 0 and "共 8 个样本，2 个 bot，2 道题" in text and f"written: {tmp_path / 's' / 'again.json'}" in text
    assert json.loads((tmp_path / "s" / "again.json").read_text(encoding="utf-8")) == json.loads((out / "summary.json").read_text(encoding="utf-8"))
    code, text = run(["summarize", "--results", str(tmp_path / "missing.jsonl")], capsys)
    assert code == 1 and "里没有结果" in text


# ------------------------------------------------------------------------------------------------ replay


@pytest.fixture()
def saved(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    game = play_long_game()
    path = tmp_path / "data" / "games" / "g1" / "game.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(game, ensure_ascii=False), encoding="utf-8")
    return path, game


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_replay_says_whether_a_saved_game_can_be_rebuilt_and_leaves_it_alone(saved: tuple[Path, dict[str, Any]], capsys: pytest.CaptureFixture[str]) -> None:
    path, game = saved
    before = digest(path)
    code, text = run(["replay", "--game", str(path)], capsys)
    assert code == 0 and "重放 g1：账本" in text and "终局世界状态（玩家/频道/请求/能力/阶段/计数）：完全一致" in text and "结论：可行" in text
    code, text = run(["replay", "--game", str(path), "--upto", "30"], capsys)
    assert code == 0 and "到 #30" in text and "结论：可行" in text
    code, text = run(["replay", "--data-dir", str(path.parents[2])], capsys)  # (every game below <dir>/games, one line each)
    assert code == 0 and text.startswith("g1 v0.13.1 账本 ") and "终局状态 一致；引擎拒绝 0 步；转不回的条目 0 个" in text
    code, text = run(["replay", "--game", str(path), "--spec", "current", "--deal", "explicit"], capsys)
    assert code == 1 and "random.drawn" in text and "有缺口" in text  # (an explicit deal does not use the generator: the later random draws differ)
    assert digest(path) == before  # (read only)


def test_replay_lists_where_a_seat_is_asked_to_act(saved: tuple[Path, dict[str, Any]], capsys: pytest.CaptureFixture[str]) -> None:
    path, game = saved
    code, text = run(["replay", "--game", str(path), "--list-decisions"], capsys)
    lines = text.splitlines()
    assert code == 0 and len(lines) == len(decision_points(game)) and any(" request " in line and "请投票" in line for line in lines) and any(" floor " in line and "→ N1" in line for line in lines)


def test_replay_writes_a_situation_script_that_the_runner_accepts(saved: tuple[Path, dict[str, Any]], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path, game = saved
    point = next(p for p in decision_points(game) if p["kind"] == "request")
    emitted = tmp_path / "scripts" / "from-save.yaml"
    code, text = run(["replay", "--game", str(path), "--upto", str(point["seq"]), "--seat", "N3", "--id", "from-save", "--emit", str(emitted)], capsys)
    assert code == 0 and f"written: {emitted}" in text and emitted.read_text(encoding="utf-8").startswith("# 由存档重放得到")
    situation = load_situation(emitted)
    assert situation.id == "from-save" and situation.decision.seats == ("N3",) and situation.meta["upto_seq"] == point["seq"] and situation.scenario == "minimal_test"
    code, text = run(["situations", "--situations", str(emitted), "--check"], capsys)
    assert code == 0 and "✔ N3" in text and "叫醒它的是 request.opened" in text
    out = tmp_path / "out"
    code, _ = run(["run", "--situations", str(emitted), "--bots", "legacy,bot:Alice", "--out", str(out)], capsys)
    assert code == 0 and [r["status"] for r in rows_in(out)] == ["ok", "ok"] and {r["decision"]["tool"] for r in rows_in(out)} == {"respond"}


@pytest.mark.parametrize(
    ("extra", "text"),
    [
        (["--emit", "OUT"], "--emit 只能用于一局（--game），并且需要 --upto"),
        (["--emit", "OUT", "--upto", "30", "--seat", "N3"], "并且需要 --upto"),
        (["--emit", "OUT", "--upto", "30", "--seat", "N3", "--id", "Bad Id"], "写不出合格的题目"),
        (["--emit", "OUT", "--upto", "30", "--seat", "N9", "--id", "ok-id"], "座位 N9 不在这局里"),
    ],
)
def test_replay_emit_refuses_what_it_cannot_write_well(extra: list[str], text: str, saved: tuple[Path, dict[str, Any]], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path, _ = saved
    emitted = tmp_path / "scripts" / "x.yaml"
    code, printed = run(["replay", "--game", str(path), *[str(emitted) if a == "OUT" else a for a in extra]], capsys)
    assert code == 2 and text in printed and not emitted.exists()


def test_replay_does_not_write_a_script_from_a_game_it_cannot_reproduce(saved: tuple[Path, dict[str, Any]], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path, game = saved
    broken = json.loads(path.read_text(encoding="utf-8"))
    entry = next(e for e in broken["world"]["ledger"] if e["type"] == "request.closed")
    entry["data"]["tally"]["N3"] = 99  # (the engine's own bookkeeping differs from what the replay makes)
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
    emitted = tmp_path / "scripts" / "x.yaml"
    seq = entry["seq"]
    code, text = run(["replay", "--game", str(path), "--upto", str(seq), "--seat", "N3", "--id", "x", "--emit", str(emitted)], capsys)
    assert code == 1 and "tally.N3" in text and "没有写出题目" in text and not emitted.exists()


def test_replay_reports_games_it_cannot_read_and_needs_something_to_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, text = run(["replay", "--game", str(tmp_path / "nowhere" / "game.json")], capsys)
    assert code == 1 and "读不了" in text
    code, text = run(["replay"], capsys)
    assert code == 2 and "要给 --game 或 --data-dir" in text
    empty = tmp_path / "e" / "games" / "g0" / "game.json"
    empty.parent.mkdir(parents=True)
    empty.write_text(json.dumps({"world": {"ledger": []}}), encoding="utf-8")
    code, text = run(["replay", "--data-dir", str(tmp_path / "e")], capsys)
    assert code == 0 and "g0：账本是空的，跳过" in text
