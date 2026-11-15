"""Streaming accumulation: same output as before, without rescanning every chunk."""

from __future__ import annotations

import json
import random
import unittest
from typing import Any
from unittest.mock import patch

import app.llm.types as llm_module  # the stream view looks split_inline_think up here
from app.engine.llm import LLMClient, split_inline_think
from test_llm_support import CHAT_SDK, SharedScriptFactory, base_cfg, make_chunk, tc_delta

MESSAGES = [{"role": "user", "content": "hi"}]


async def run_stream(
    chunks: list[Any], *, with_callback: bool = True
) -> tuple[Any, list[tuple[str, str]]]:
    factory = SharedScriptFactory([chunks])
    llm = LLMClient(base_cfg())
    seen: list[tuple[str, str]] = []

    async def on_chunk(thought: str, content: str) -> None:
        seen.append((thought, content))

    with patch(CHAT_SDK, new=factory):
        result = await llm.chat_stream(MESSAGES, on_chunk=on_chunk if with_callback else None)
    return result, seen


def reference_views(chunks: list[Any]) -> list[tuple[str, str]]:
    """What the old implementation reported: split_inline_think over the full text so far."""
    raw, native = "", ""
    views: list[tuple[str, str]] = []
    for chunk in chunks:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.reasoning_content:
            native += delta.reasoning_content
        if delta.content:
            raw += delta.content
        views.append(split_inline_think(raw, native))
    return views


def split_text(text: str, rng: random.Random) -> list[str]:
    pieces: list[str] = []
    i = 0
    while i < len(text):
        n = rng.randint(1, 6)
        pieces.append(text[i : i + n])
        i += n
    return pieces


class StreamOutputEquivalenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_content_and_native_reasoning(self) -> None:
        chunks = [
            make_chunk(None, reasoning="  let me "),
            make_chunk(None, reasoning="think  "),
            make_chunk("  Hello"),
            make_chunk(", "),
            make_chunk("world!  ", finish="stop"),
            make_chunk(no_choices=True, usage=(11, 7)),
        ]
        result, seen = await run_stream(chunks)
        self.assertEqual(seen, reference_views(chunks))
        self.assertEqual(result.thought, "let me think")
        self.assertEqual(result.content, "Hello, world!")
        self.assertEqual((result.prompt_tokens, result.completion_tokens), (11, 7))
        self.assertEqual(result.finish_reason, "stop")

    async def test_inline_think_tags_including_split_markers(self) -> None:
        text = "intro <think>secret plan</think> visible answer <think>more</think> end"
        rng = random.Random(7)
        for trial in range(25):
            pieces = split_text(text, rng)
            chunks = [make_chunk(p) for p in pieces]
            result, seen = await run_stream(chunks)
            self.assertEqual(seen, reference_views(chunks), msg=f"pieces={pieces}")
            self.assertEqual(result.thought, "secret plan\nmore")
            self.assertEqual(result.content, "intro  visible answer  end")

    async def test_unclosed_think_tag_midstream(self) -> None:
        chunks = [make_chunk("before <thi"), make_chunk("nk>still thin"), make_chunk("king")]
        result, seen = await run_stream(chunks)
        self.assertEqual(seen, reference_views(chunks))
        self.assertEqual(seen[0], ("", "before <thi"))  # marker not complete yet
        self.assertEqual(seen[1], ("still thin", "before"))
        self.assertEqual(result.thought, "still thinking")

    async def test_think_tag_plus_native_reasoning(self) -> None:
        chunks = [
            make_chunk(None, reasoning="native"),
            make_chunk("<think>inline</think>answer"),
        ]
        result, seen = await run_stream(chunks)
        self.assertEqual(seen, reference_views(chunks))
        self.assertEqual(result.thought, "native\ninline")
        self.assertEqual(result.content, "answer")

    async def test_dsml_markup_is_stripped_from_content_and_parsed_as_tool_call(self) -> None:
        dsml = (
            "Okay.<｜DSML｜function_calls><｜DSML｜invoke name=\"speak\">"
            "<｜DSML｜parameter name=\"text\">hello</｜DSML｜parameter>"
            "</｜DSML｜invoke></｜DSML｜function_calls>"
        )
        rng = random.Random(3)
        chunks = [make_chunk(p) for p in split_text(dsml, rng)]
        result, seen = await run_stream(chunks)
        self.assertEqual(seen, reference_views(chunks))
        self.assertEqual(result.content, "Okay.")
        self.assertEqual([(t.name, json.loads(t.arguments_json)) for t in result.tool_calls],
                         [("speak", {"text": "hello"})])

    async def test_random_streams_match_reference_at_every_step(self) -> None:
        rng = random.Random(2024)
        vocab = ["a", " b", "<think>", "</think>", "<thi", "nk>", "DSML", "<｜", "x\n", " ", "中文", "<|DSML|calls>"]
        for _ in range(60):
            chunks = []
            for _ in range(rng.randint(1, 25)):
                if rng.random() < 0.25:
                    chunks.append(make_chunk(None, reasoning=rng.choice(vocab)))
                elif rng.random() < 0.1:
                    chunks.append(make_chunk(no_choices=True))
                else:
                    chunks.append(make_chunk(rng.choice(vocab)))
            _, seen = await run_stream(chunks)
            self.assertEqual(seen, reference_views(chunks))

    async def test_without_callback_result_is_identical(self) -> None:
        chunks = [make_chunk("<think>t</think>"), make_chunk("done", finish="stop")]
        with_cb, _ = await run_stream(chunks)
        no_cb, seen = await run_stream(chunks, with_callback=False)
        self.assertEqual(seen, [])
        self.assertEqual((no_cb.thought, no_cb.content), (with_cb.thought, with_cb.content))

    async def test_callback_fires_for_every_chunk_with_choices(self) -> None:
        chunks = [
            make_chunk("a"),
            make_chunk(no_choices=True, usage=(1, 1)),
            make_chunk(tool_calls=[tc_delta(0, id="c1", name="f", args="{}")]),
            make_chunk(None, finish="stop"),
        ]
        _, seen = await run_stream(chunks)
        self.assertEqual(len(seen), 3)  # the no-choices (usage) chunk is skipped


class StreamLinearityTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_think_pass_per_chunk_for_plain_text(self) -> None:
        chunks = [make_chunk(f"word{i} ") for i in range(3000)]
        chunks += [make_chunk(None, reasoning=f"r{i} ") for i in range(1000)]
        with patch.object(llm_module, "split_inline_think", wraps=split_inline_think) as spy:
            result, seen = await run_stream(chunks)
        self.assertEqual(len(seen), 4000)
        # only the final pass at the end of the stream
        self.assertEqual(spy.call_count, 1)
        self.assertTrue(result.content.startswith("word0 word1"))
        self.assertTrue(result.thought.endswith("r999"))

    async def test_think_pass_only_starts_after_a_marker_appears(self) -> None:
        chunks = [make_chunk("plain ") for _ in range(200)]
        chunks += [make_chunk("<think>inner"), make_chunk(" more"), make_chunk("</think>end")]
        with patch.object(llm_module, "split_inline_think", wraps=split_inline_think) as spy:
            result, _ = await run_stream(chunks)
        # 3 marker-affected chunks + 1 final pass; the 200 plain chunks cost nothing
        self.assertEqual(spy.call_count, 4)
        self.assertEqual(result.thought, "inner more")
        self.assertTrue(result.content.endswith("end"))

    async def test_no_snapshot_work_without_callback(self) -> None:
        chunks = [make_chunk(f"w{i} ") for i in range(2000)]
        with patch.object(llm_module, "split_inline_think", wraps=split_inline_think) as spy:
            result, _ = await run_stream(chunks, with_callback=False)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(result.content.split()[0], "w0")
        self.assertEqual(len(result.content.split()), 2000)

    async def test_marker_split_across_chunks_is_detected(self) -> None:
        for cut in range(1, len("<think>")):
            chunks = [make_chunk("ab<think>"[: 2 + cut]), make_chunk("<think>"[cut:] + "x")]
            _, seen = await run_stream(chunks)
            self.assertEqual(seen, reference_views(chunks), msg=f"cut={cut}")

    async def test_large_stream_completes_with_exact_content(self) -> None:
        pieces = [f"{i}," for i in range(20000)]
        result, seen = await run_stream([make_chunk(p) for p in pieces], with_callback=False)
        self.assertEqual(result.content, "".join(pieces).strip())


class ToolCallStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_arguments_are_concatenated_per_index(self) -> None:
        chunks = [
            make_chunk(tool_calls=[tc_delta(0, id="call_a", name="vote", args='{"tar')]),
            make_chunk(tool_calls=[tc_delta(1, id="call_b", name="note", args='{"x":')]),
            make_chunk(tool_calls=[tc_delta(0, args='get": 3}')]),
            make_chunk(tool_calls=[tc_delta(1, args=' 1}')], finish="tool_calls"),
        ]
        result, _ = await run_stream(chunks)
        self.assertEqual([t.id for t in result.tool_calls], ["call_a", "call_b"])
        self.assertEqual([t.name for t in result.tool_calls], ["vote", "note"])
        self.assertEqual(result.tool_calls[0].parse_args(), {"target": 3})
        self.assertEqual(result.tool_calls[1].parse_args(), {"x": 1})
        self.assertEqual(result.finish_reason, "tool_calls")

    async def test_tool_calls_are_ordered_by_index_and_ids_default(self) -> None:
        chunks = [
            make_chunk(tool_calls=[tc_delta(2, name="c", args="{}")]),
            make_chunk(tool_calls=[tc_delta(0, name="a", args="{}")]),
        ]
        result, _ = await run_stream(chunks)
        self.assertEqual([(t.id, t.name) for t in result.tool_calls], [("call_0", "a"), ("call_2", "c")])

    async def test_many_argument_fragments(self) -> None:
        body = json.dumps({"text": "x" * 5000})
        frags = [body[i : i + 3] for i in range(0, len(body), 3)]
        chunks = [make_chunk(tool_calls=[tc_delta(0, id="c", name="say", args=frags[0])])]
        chunks += [make_chunk(tool_calls=[tc_delta(0, args=f)]) for f in frags[1:]]
        result, _ = await run_stream(chunks, with_callback=False)
        self.assertEqual(result.tool_calls[0].parse_args(), {"text": "x" * 5000})

    async def test_usage_is_estimated_when_the_server_reports_none(self) -> None:
        result, _ = await run_stream([make_chunk("some answer text")])
        self.assertGreater(result.prompt_tokens, 0)
        self.assertGreater(result.completion_tokens, 0)


if __name__ == "__main__":
    unittest.main()
