"""The question bank (``bench/fixtures``): it is what the generators write, every question builds on the real engine and reaches its decision point, every
``truth`` agrees with the world it is about, and the families have the sizes the plan asks for.

The situations are *static* files (a reader sees exactly what a bot will be asked); ``bench/cases`` is the author's tool that writes them, and the first test
below is what keeps the two from drifting apart.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

import pytest
import yaml
from bench_helpers import play_long_game

from bench.authoring import seat_number
from bench.cases import all_cases, l4, leaks, render
from bench.play import build_scene
from bench.replay import convert_ledger, decision_points, replay_game, situation_from_game
from bench.situation import DEFAULT_NAMES, Situation, load_situation

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "bench" / "fixtures"
SAVED_GAMES_ENV = "ARENA_BENCH_SAVED_GAMES"
FILES = sorted(p for p in (FIXTURES / "situations").glob("*.yaml") if not p.name.startswith("_"))
BY_SUITE: dict[str, list[Path]] = {}
for _path in FILES:
    BY_SUITE.setdefault(_path.name.split("-", 1)[0].upper(), []).append(_path)

#: What the plan / the lead asked of each family (a minimum): the number of situation files (pairs for L1).
MINIMUM = {"L2": 6, "L3": 8, "M3": 24, "M2": 6, "S2": 8, "R": 4, "L4": 4}


def _visible(scene, seat: str) -> str:
    """Everything the seat under test can read at its decision point: the hard-fact table and every entry it may see (messages, announcements, its own messages)."""
    board = scene.world.board(seat)
    return "\n".join([f["text"] for f in board["facts"]] + [json.dumps(e.data, ensure_ascii=False) for e in scene.world.entries_visible(seat)])


def _header(path: Path) -> str:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            break
        lines.append(line[1:].strip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------------- the files
def test_the_committed_bank_is_what_the_generators_write() -> None:
    assert render.check() == []  # (python -m bench fixtures rewrites them)


def test_every_generated_case_is_a_committed_file_and_nothing_generated_is_missing_from_the_suites() -> None:
    paths = {case.path for case in all_cases()}
    assert paths <= {f"situations/{p.name}" for p in FILES}
    assert len(all_cases()) == len({c.id for c in all_cases()})  # (an id names one case)
    for case in all_cases():
        assert case.id == Path(case.path).stem and case.id.split("-", 1)[0].upper() == case.suite


@pytest.mark.parametrize("suite, least", sorted(MINIMUM.items()))
def test_each_family_has_the_size_the_plan_asks_for(suite: str, least: int) -> None:
    assert len(BY_SUITE.get(suite, [])) >= least, (suite, len(BY_SUITE.get(suite, [])))


def test_l1_has_at_least_six_pairs_and_each_pair_points_at_two_files_that_differ_only_in_how_two_players_talk() -> None:
    pairs = sorted((FIXTURES / "pairs").glob("l1-*.yaml"))
    assert len(pairs) >= 6
    for pair in pairs:
        body = yaml.safe_load(pair.read_text(encoding="utf-8"))
        assert body["format"] == "arena-pair" and body["suite"] == "L1" and body["id"] == pair.stem
        a, b = (yaml.safe_load((FIXTURES / body[side]).read_text(encoding="utf-8")) for side in ("a", "b"))
        assert (a["meta"], b["meta"]) == ({"pair": pair.stem, "variant": "A"}, {"pair": pair.stem, "variant": "B"})
        # everything but the text of the steps marked ``meta: {styled: true}`` is the same: the facts, the votes, the order, the clock
        assert {k: v for k, v in a.items() if k not in ("steps", "id", "title", "tags", "truth", "brief", "judge", "meta")} == {
            k: v for k, v in b.items() if k not in ("steps", "id", "title", "tags", "truth", "brief", "judge", "meta")}
        assert len(a["steps"]) == len(b["steps"])
        differing = [(x, y) for x, y in zip(a["steps"], b["steps"]) if x != y]
        assert 2 <= len(differing) <= 4, (pair.stem, len(differing))
        for x, y in differing:
            assert x.get("meta") == {"styled": True} == y.get("meta")  # only the styled speeches differ
            assert list(x["say"]) == list(y["say"])  # ... of the same player
        sides = {"a": a["truth"]["sides"], "b": b["truth"]["sides"]}
        assert sides["a"] == {"fluent": sides["b"]["blunt"], "blunt": sides["b"]["fluent"]}  # the two swap
        assert a["truth"]["answer"] == b["truth"]["answer"] == body["answer"]  # the decisive fact, and so the answer, is the same
        assert a["truth"]["answer"][0] == (sides["a"]["fluent"] if a["truth"]["roles"][sides["a"]["fluent"]] == "werewolf" else sides["a"]["blunt"])
        fluent_a = next(s for s in a["steps"] if sides["a"]["fluent"] in s.get("say", {}) and s.get("meta"))["say"][sides["a"]["fluent"]]
        blunt_a = next(s for s in a["steps"] if sides["a"]["blunt"] in s.get("say", {}) and s.get("meta"))["say"][sides["a"]["blunt"]]
        assert len(fluent_a) > 2 * len(blunt_a), (pair.stem, len(fluent_a), len(blunt_a))  # the fluent side really talks more (and so the bias can be measured)


# ---------------------------------------------------------------------------------------------------------------------- every question
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_a_question_builds_on_the_real_engine_and_its_truth_agrees_with_the_world(path: Path) -> None:
    situation: Situation = load_situation(path)
    scene = build_scene(situation, seat_name="Alice")
    seat = situation.decision.seat
    board = scene.world.board(seat)
    assert [e.payload["event"] for e in scene.wake] in (["request.opened"], ["floor.granted"])  # the decision point: a request is opened, or the floor is given
    truth = situation.truth
    open_requests = [r for r in board["requests"] if not r["answered"]]
    if truth.get("request"):
        assert len(open_requests) == 1, "the seat has exactly one question open"
        request = open_requests[0]
        options = [o["id"] if isinstance(o, dict) else o for o in request.get("options") or []]
        assert truth["answer"], "a question has a right answer"
        if truth.get("match") == "text":
            assert request["kind"] == "text" and not options
            assert not set(truth["answer"]) & set(truth.get("wrong", []))
        else:
            assert request["kind"] == "choice"
            assert len(options) == len(set(options)) >= 2
            assert set(truth["answer"]) <= set(options), (truth["answer"], options)  # truth ∈ options
            assert not set(truth.get("wrong", [])) & set(truth["answer"]) and set(truth.get("wrong", [])) <= set(options)
        score = situation.score
        assert len(score) == 1 and score[0]["request"] == truth["request"]  # (the truth is the objective score)
        text = _visible(scene, seat)
        for evidence in truth.get("evidence", []):
            assert evidence in text, f"{path.stem}: the evidence {evidence!r} is not something {seat} can read"
        assert truth.get("decisive"), "the decisive fact is said in words (for the judge and the header)"
    else:
        assert not open_requests and situation.decision.expect  # (a speech: the floor)
    # the author's promises: a header that says what is tested and where the trap is, tags and a title
    header = _header(path)
    assert "在测什么" in header and re.search(r"陷阱|错误选项|干扰", header), f"{path.stem}: the header says what it tests and where the trap is"
    assert situation.title and situation.tags and situation.id == path.stem


def test_a_decisive_fact_in_a_vote_question_names_a_wolf_and_every_answer_is_a_seat_that_is_still_in_the_game() -> None:
    for suite in ("L1", "L2", "L3"):
        for path in BY_SUITE[suite]:
            situation = load_situation(path)
            roles = situation.roles
            by_seat = {f"N{i + 1}": role for i, role in enumerate(roles)}
            scene = build_scene(situation, seat_name="Alice")
            options = {o["id"] if isinstance(o, dict) else o for o in next(r for r in scene.world.board(situation.decision.seat)["requests"] if not r["answered"])["options"]}
            for answer in situation.truth["answer"]:
                assert by_seat[answer] in ("werewolf", "white_wolf") and answer in options, (path.stem, answer)  # the right vote is a wolf who is alive


# ---------------------------------------------------------------------------------------------------------------------- the families
def test_l2_questions_make_the_trivia_a_red_herring_and_say_which_they_are() -> None:
    for path in BY_SUITE["L2"]:
        situation = load_situation(path)
        trivia = situation.judge["L2"]["trivia"]
        assert trivia and all(isinstance(t, str) and t for t in trivia)
        assert "trivia" in situation.tags and situation.truth["trap"].startswith("两边在无关紧要的地方不一样")
        bystander = situation.meta.get("bystander")
        if bystander:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            spoken = [next(iter(s["say"])) for s in data["steps"] if "say" in s]
            claimers = [seat for seat in spoken if any(seat in s.get("say", {}) and s.get("meta") for s in data["steps"])]
            last = {seat: max(i for i, who in enumerate(spoken) if who == seat) for seat in set(spoken)}
            assert last[bystander] > max(last[c] for c in set(claimers))  # (he remarks on what the two have just said: his last turn is after theirs)


def test_m3_has_each_answer_position_about_equally_often_and_wrong_options_that_are_not_the_right_one() -> None:
    positions: Counter[str] = Counter()
    for path in BY_SUITE["M3"]:
        situation = load_situation(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        request = next(s for s in data["steps"] if s.get("tool") == "open_request")["args"]
        labels = [o["label"] for o in request["options"]]
        assert len(labels) == len(set(labels)) == 4, path.stem
        positions[situation.truth["answer"][0]] += 1
        assert situation.truth.get("rule"), "the rule it quotes"
    assert set(positions) == set("abcd") and max(positions.values()) - min(positions.values()) <= 4, positions  # (the position of the answer tells nothing)


def test_m2_is_a_long_game_with_a_question_that_only_the_fact_table_can_still_answer_at_six_thousand_characters() -> None:
    for path in BY_SUITE["M2"]:
        situation = load_situation(path)
        scene = build_scene(situation, seat_name="Alice")
        assert len(scene.world.ledger) >= 330, path.stem  # four days: far more than a bot's history window of 12000 characters shows... or fits
        assert situation.meta["asked_about"] in path.stem and situation.truth["kind"] == "recall"
        board = scene.world.board(situation.decision.seat)
        facts = "\n".join(f["text"] for f in board["facts"])
        assert all(e in facts for e in situation.truth["evidence"]), path.stem  # every answer is in the hard-fact table, which the history window does not cut
    kinds = Counter(load_situation(p).meta["kind"] for p in BY_SUITE["M2"])
    assert kinds["text"] >= 2 and kinds["choice"] >= 4  # text questions (answered in words) and choice questions


def test_s2_and_r_are_turns_to_speak_with_nothing_in_the_situation_that_says_what_to_say() -> None:
    assert len(BY_SUITE["S2"]) >= 8
    for path in BY_SUITE["S2"] + BY_SUITE["R"]:
        situation = load_situation(path)
        assert situation.decision.expect == ("speak",) and not situation.truth and not situation.score, path.stem  # (no right answer: the judge matches the style)
        assert situation.id.startswith("s2-") == (situation.judge == {"S2": {}}), path.stem  # (the S2 turns are judged by style cards; the R turns are not judged at all)
        scene = build_scene(situation, seat_name="Alice")
        public = next(c for c in scene.world.board(situation.decision.seat)["channels"] if c["id"] == "public")
        assert public["can_write"] and public["floor"]["mine"]["messages_left"] == 1, path.stem  # (the floor is the seat's, for one message)
        if path.stem.startswith("r-"):
            assert public["floor"]["mine"]["chars_per_message"] == situation.meta["max_chars"] < 300  # (the limit that the file promises is the limit of the turn)
        else:
            assert public["floor"]["mine"]["chars_per_message"] == 300 and "风格" in _header(path)
    assert len({load_situation(p).meta["moment"] for p in BY_SUITE["S2"]}) == len(BY_SUITE["S2"])  # (nine different moments)


def test_the_seat_under_test_never_says_a_line_the_author_wrote_before_the_decision() -> None:
    for path in FILES:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        seat = (data["decision"].get("seat") or data["decision"]["seats"][0])
        for step in data["steps"]:
            if "say" in step:
                assert seat not in step["say"], f"{path.stem}: {seat} speaks in the script"
            elif step.get("as") == seat:
                assert step.get("tool") in ("respond", "pass"), f"{path.stem}: {seat} does {step.get('tool')} in the script"  # (night actions and passes only)


def test_no_situation_names_anybody_but_the_default_players_and_no_file_has_a_seat_that_is_not_one_of_twelve() -> None:
    from bench.situation import DEFAULT_NAMES

    allowed = set(DEFAULT_NAMES[:12])
    for path in FILES:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["players"] == {"count": 12, "names": list(DEFAULT_NAMES[:12])} and set(data["players"]["names"]) == allowed
        text = path.read_text(encoding="utf-8")
        assert not [m for m in re.findall(r"N(\d+)", text) if not 1 <= int(m) <= 12], path.stem
        assert seat_number("N12") == 12


# ---------------------------------------------------------------------------------------------------------------------- L4: real moments, rebuilt with every word invented
def _roles_of(situation: Situation) -> list[str]:
    roles = situation.roles
    return list(roles.values()) if isinstance(roles, dict) else list(roles)


def test_l4_has_five_real_moments_from_three_games_and_each_is_one_the_real_table_got_wrong() -> None:
    files = BY_SUITE["L4"]
    assert len(files) >= 4
    sources = Counter(load_situation(p).meta["source"] for p in files)
    assert len(sources) == 3 and set(sources) == set(l4.ROLES)  # (three different games)
    for path in files:
        situation = load_situation(path)
        meta, truth = situation.meta, situation.truth
        assert {"source", "seq", "real_choice", "real_ballots", "winner"} <= set(meta) and meta["winner"] == l4.WINNER[meta["source"]]
        roles = _roles_of(situation)
        assert roles == l4.ROLES[meta["source"]]  # the identities of the saved game, seat by seat
        assert roles[seat_number(situation.decision.seat) - 1] == "villager", "the seat under test is a plain villager: no private information, no role to play"
        assert truth["kind"] == "side" and truth["source"].startswith("本机存档") and truth["decisive"].startswith("存档终局揭晓的真相")
        assert meta["real_choice"] in truth["wrong"], path.stem  # at this moment the real seat sided with the wrong camp: that is why the moment is in the suite
        assert {"L4", "real", "side"} <= set(situation.tags) and ({"sheriff"} <= set(situation.tags) or {"exile"} <= set(situation.tags))


def test_l4_truth_is_the_end_of_the_game_a_sheriff_vote_goes_to_the_real_seer_and_an_exile_vote_to_a_wolf() -> None:
    for path in BY_SUITE["L4"]:
        situation = load_situation(path)
        by_seat = {f"N{i + 1}": role for i, role in enumerate(_roles_of(situation))}
        wolves = {seat for seat, role in by_seat.items() if role in ("werewolf", "white_wolf")}
        board = build_scene(situation, seat_name="Alice").world.board(situation.decision.seat)
        alive = {p["id"] for p in board["players"] if "存活" in p.get("tags", [])}
        answer, wrong = set(situation.truth["answer"]), set(situation.truth["wrong"])
        if "sheriff" in situation.tags:
            assert [by_seat[s] for s in answer] == ["seer"] and wrong <= wolves, path.stem  # (the true seer; the wrong ones are wolves, the white wolf who claims nothing among them)
            assert situation.truth["request"] == "@badge"
        else:
            assert answer == wolves & alive and wrong == alive - wolves, path.stem  # (any wolf is right; every good player - the seat under test too - is wrong)
            assert situation.truth["request"] == "@vote"


def test_l4_the_other_ballots_are_the_real_ones_and_the_seat_under_test_has_not_voted() -> None:
    for path in BY_SUITE["L4"]:
        situation = load_situation(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        cast = next(step for step in data["steps"] if step.get("answer") == situation.truth["request"])["values"]
        assert cast == situation.meta["real_ballots"] and situation.decision.seat not in cast, path.stem
        assert len(cast) >= 8  # (nine voters at a sheriff vote, twelve at an exile vote: everyone but the seat under test)


# ---------------------------------------------------------------------------------------------------------------------- desensitization: no word of a saved game
def test_the_leak_check_catches_a_run_of_shared_words_and_lets_a_paraphrase_pass() -> None:
    said = "各位，我是3号位的猎人，今晚按兵不动，等天亮看过票型再决定要不要开枪。"  # (an invented line standing for a saved game's: the check is proved on a sample, never on real text)
    index = {gram: "sample g1" for gram in leaks.grams(said)}
    copy = leaks.find_leaks([("copy", "各位，我是 3 号位的猎人——今晚按兵不动、等天亮看过票型，再决定要不要开枪！")], index)
    assert [(leak.where, leak.source) for leak in copy] == [("copy", "sample g1")] and copy[0].length >= 20  # (other punctuation, spacing, case: still a leak)
    assert leaks.find_leaks([("partial", "我想说：" + "今晚按兵不动等天亮看过票型再决定" + "，大家觉得呢")], index)  # (a clause of it inside another sentence)
    assert leaks.find_leaks([("paraphrase", "我是三号，身份是猎人；这一晚我不打算出手，白天先听大家怎么投，之后才决定。")], index) == []  # (the same thing in other words)
    ten = "甲乙丙丁戊己庚辛壬癸"
    one = {gram: "x" for gram in leaks.grams(ten)}
    assert len(leaks.find_leaks([("ten", "前" + ten + "后")], one)) == 1 and leaks.find_leaks([("nine", ten[:9])], one) == []  # (exactly n characters is a leak, n - 1 is not)
    assert leaks.normalize("Ａ，b。 我\n们 **好**") == "ａb我们好" or leaks.normalize("A, b. 我们 好") == "ab我们好"


def test_the_leak_check_reads_what_players_write_and_not_what_the_moderator_writes() -> None:
    steps = [
        {"as": "moderator", "tool": "send_message", "args": {"content": "系统文字"}},
        {"say": {"N3": "我先说。"}, "channel": "public"},
        {"as": "N4", "tool": "speak", "args": {"channel_id": "public", "content": "公开发言"}},
        {"as": "N4", "tool": "speak_template", "args": {"template_id": "vote_declare", "params": {"target": "N5", "reason": "他发言没东西"}}},
        {"as": "N5", "tool": "respond", "args": {"request_id": "r1", "value": "N6", "reason": "私下的理由"}},
        {"as": "N5", "tool": "respond", "args": {"request_id": "r2", "value": "这是对一个文字请求的回答，写得比较长"}},
        {"as": "N5", "tool": "use_ability", "args": {"ability_id": "x", "params": {"target": "N6", "declaration": "自爆宣言"}}},
        {"answer": "@vote", "values": {"N1": "N2"}, "reasons": {"N1": "投票理由"}},
    ]
    assert sorted(text for _where, text in leaks.texts_of_steps(steps)) == sorted(
        ["我先说。", "公开发言", "他发言没东西", "私下的理由", "这是对一个文字请求的回答，写得比较长", "自爆宣言", "投票理由"])  # (no seat, no side, nothing of the moderator's)


def test_the_leak_check_reads_a_saved_game_the_way_it_is_saved_and_finds_a_copy_of_a_speech_in_a_bank_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    game = play_long_game()
    texts = [text for _where, text in leaks.texts_of_game(game)]
    for said in ("我先说两句：今天没有什么特别的怀疑。", "先投一个", "红队万岁", "第 1 轮的投票规则有误。", "自由讨论里的一句话。"):
        assert said in texts, said
    assert "你们是红队。" not in texts and "一局结束" not in texts  # (the moderator's words are the manual's, the same in every game)
    games_dir = tmp_path / "data" / "games" / "g1"
    games_dir.mkdir(parents=True)
    (games_dir / "game.json").write_text(json.dumps(game, ensure_ascii=False), encoding="utf-8")
    bank = tmp_path / "fx"
    (bank / "situations").mkdir(parents=True)
    (bank / "pairs").mkdir()
    clean = {"format": "arena-situation", "id": "l9-clean", "title": "x", "steps": [{"say": {"N1": "完全是另外一回事的一句话，和任何存档都没有关系。"}, "channel": "public"}]}
    (bank / "situations" / "l9-clean.yaml").write_text("# 在测什么：x\n" + yaml.safe_dump(clean, allow_unicode=True), encoding="utf-8")
    assert leaks.main(["--games", str(tmp_path / "data"), "--fixtures", str(bank), "--n", "8", "--generated-n", "8"]) == 0 and "没有泄漏" in capsys.readouterr().out
    copied = {**clean, "id": "l9-copy", "steps": [{"say": {"N2": "我先说两句：今天没有什么特别的怀疑，所以我听听大家的。"}, "channel": "public"}]}
    (bank / "situations" / "l9-copy.yaml").write_text("# 在测什么：x\n" + yaml.safe_dump(copied, allow_unicode=True), encoding="utf-8")
    assert leaks.main(["--games", str(tmp_path / "data"), "--fixtures", str(bank), "--n", "8", "--generated-n", "8"]) == 1
    out = capsys.readouterr().out
    assert "l9-copy.yaml: step 0: N2 says" in out and "l9-clean" not in out and "1 处泄漏" in out
    assert leaks.main(["--games", str(tmp_path / "nothing"), "--fixtures", str(bank)]) == 0 and "没有存档" in capsys.readouterr().out  # (nothing to compare with is not a failure)


def test_the_leak_check_covers_the_whole_text_of_a_bank_file_the_comment_the_author_fields_and_the_lines_of_the_players() -> None:
    path = BY_SUITE["L4"][0]
    where = [w for w, _t in leaks.texts_of_file(path)]
    assert any(w.endswith("header") for w in where) and any("truth.decisive" in w for w in where) and any("truth.trap" in w for w in where) and any("title" in w for w in where)
    assert any("says" in w for w in where) and any("brief" in w for w in where)
    files = leaks.bank_files(FIXTURES)
    assert files and not any(p.name.startswith("_") for p in files) and {p.parent.name for p in files} == {"situations", "pairs"}


def _saved_games() -> dict[str, dict]:
    games: dict[str, dict] = {}
    for base in (ROOT / "data", ROOT / ".demo-data" / "real"):
        games.update(leaks.load_games(base))
    return games


opt_in = pytest.mark.skipif(
    os.environ.get(SAVED_GAMES_ENV) != "1",
    reason=f"opt-in: it reads the user's own saved games (read only) - run it with {SAVED_GAMES_ENV}=1 on a machine that has some",
)


@opt_in
def test_the_bank_holds_no_text_and_no_name_of_the_saved_games_of_this_machine() -> None:
    """The desensitization check itself, on the real games (read only): not one run of ten characters of what a player said in a saved game is in a committed file
    (the lines of the situations, the comments, the author's fields), and no name a person chose for himself.  Off by default: the games are the user's own data."""
    games = _saved_games()
    if not games:
        pytest.skip("no saved games on this machine")
    found = leaks.scan_bank(FIXTURES, games)
    assert not found, "\n".join(str(leak) for leak in found)
    foreign = {name for game in games.values() for name in leaks.foreign_names(game, DEFAULT_NAMES)}
    blob = "\n".join(text for path in leaks.bank_files(FIXTURES) for _where, text in leaks.texts_of_file(path))
    assert not [name for name in foreign if name in blob], foreign


@opt_in
def test_every_l4_situation_is_the_structure_of_its_saved_game_roles_votes_and_what_the_seat_sees() -> None:
    """The reconstruction is faithful where it must be (read only): the identities, the winner, the ballots of the others and what the seat itself did are the
    game's; and at the decision point the board of the seat - the phase, who is alive and who holds the badge, the open request with its options, the
    table of hard facts (what kind of announcement, in which phase) - is the same as that of the real game replayed to that entry."""
    games = _saved_games()
    compared = 0
    for path in BY_SUITE["L4"]:
        situation = load_situation(path)
        meta = situation.meta
        # (a situation names the saved game it was cut from by the first characters of its id: the full id of a game on somebody's machine is not published)
        candidates = [g for game_id, g in games.items() if game_id.startswith(str(meta["source"]))]
        game = candidates[0] if len(candidates) == 1 else None
        if game is None:
            continue
        compared += 1
        seat = situation.decision.seat
        conversion = convert_ledger(game, upto_seq=None)
        assert conversion.roles == _roles_of(situation) and (game["world"]["state"] or {}).get("winner") == meta["winner"], path.stem
        point = next(p for p in decision_points(game) if p["seq"] == meta["seq"])
        assert seat in point["to"] and point["kind"] == "request"
        cast = {s.actor: s.args.get("value") for s in conversion.steps if s.kind == "tool" and s.tool == "respond" and s.args.get("request_id") == point["id"]}
        assert {k: v for k, v in cast.items() if k != seat} == meta["real_ballots"] and cast[seat] == meta["real_choice"], path.stem
        result = replay_game(game, upto_seq=meta["seq"])
        assert result.exact, path.stem
        real = build_scene(situation_from_game(game, upto_seq=meta["seq"], seat=seat, situation_id="real"), seat_name="Alice")
        mine = build_scene(situation, seat_name="Alice")

        def project(scene):
            board = scene.world.board(seat)
            requests = [r for r in board["requests"] if not r["answered"]]
            return {
                "phase": board.get("phase"),
                "players": {p["id"]: sorted(t for t in p.get("tags", []) if t in ("存活", "出局", "警长")) for p in board["players"]},
                "requests": [(r["kind"], r["question"][:14], [o["id"] if isinstance(o, dict) else o for o in r.get("options") or []]) for r in requests],
                "facts": [(f["kind"], f.get("template_id"), f.get("phase")) for f in board["facts"]],
                "wake": [e.payload["event"] for e in scene.wake],
            }

        assert project(mine) == project(real), path.stem
    if not compared:
        pytest.skip("none of the saved games the L4 moments were cut from is on this machine")
