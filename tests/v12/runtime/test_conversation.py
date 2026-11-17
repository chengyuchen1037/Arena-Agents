from __future__ import annotations

import json

from app.runtime.conversation import (
    Conversation,
    ConversationBook,
    message_chars,
    round_spans,
    tools_signature,
)

from .fakes import assert_paired


def user(text: str) -> dict:
    return {"role": "user", "content": text}


def assistant(call_ids: list[str], content: str | None = None) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "speak", "arguments": "{}"}} for cid in call_ids
        ],
    }


def tool(call_id: str, text: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def make(messages: list[dict]) -> Conversation:
    tools = [{"type": "function", "function": {"name": "speak"}}]
    conv = Conversation(agent_id="a1", system="SYS", tools=tools, tools_sig=tools_signature(tools))
    conv.messages.extend(messages)
    return conv


def story() -> list[dict]:
    """first(FULL), then three rounds, then an unanswered delta."""
    return [
        user("FULL"),
        user("d1"), assistant(["c1"]), tool("c1"),
        user("d2"), assistant(["c2", "c3"]), tool("c2"), tool("c3"),
        assistant(["c4"], content="thinking"), tool("c4"),
        user("tail-unanswered"),
    ]


# ------------------------------------------------------------------------------- the book


def test_book_keeps_one_conversation_per_agent_and_reset_forgets():
    book = ConversationBook()
    assert not book and len(book) == 0 and book.get("a1") is None
    a1 = book.put(make([user("x")]))
    b = Conversation(agent_id="a2", system="S", tools=[], tools_sig=tools_signature([]))
    book.put(b)
    assert book.get("a1") is a1 and book.get("a2") is b and len(book) == 2 and "a1" in book
    assert sorted(book.agents()) == ["a1", "a2"] and set(c.agent_id for c in book) == {"a1", "a2"}
    book.reset("a1")
    assert book.get("a1") is None and book.get("a2") is b
    book.reset("a1")  # forgetting twice is fine
    book.put(a1)
    book.reset()
    assert len(book) == 0 and book.get("a2") is None


def test_putting_a_conversation_again_replaces_it():
    book = ConversationBook()
    first = book.put(make([user("old")]))
    second = Conversation(agent_id="a1", system="S2", tools=[], tools_sig="x", epoch=2)
    book.put(second)
    assert book.get("a1") is second and book.get("a1") is not first and len(book) == 1


# ------------------------------------------------------------------------ the conversation


def test_request_messages_are_system_plus_the_conversation_and_a_fresh_list():
    conv = make([user("hello")])
    request = conv.request_messages()
    assert request == [{"role": "system", "content": "SYS"}, user("hello")]
    request.append(user("not stored"))
    assert len(conv) == 1  # the returned list is a copy
    assert request[1] is conv.messages[0]  # the messages themselves are shared, never copied or rewritten


def test_tail_role_and_sizes():
    conv = make([])
    assert conv.tail_role == "" and len(conv) == 0
    conv.messages.append(user("x" * 100))
    assert conv.tail_role == "user"
    assert conv.static_chars() > len("SYS") and conv.total_chars() == conv.static_chars() + message_chars(conv.messages[0])


def test_tools_signature_is_order_sensitive_and_stable():
    a = {"type": "function", "function": {"name": "a"}}
    b = {"type": "function", "function": {"name": "b"}}
    assert tools_signature([a, b]) == tools_signature([a, b]) != tools_signature([b, a])
    assert len(tools_signature([a])) == 16


def test_notes_are_handed_out_once():
    conv = make([])
    conv.notes += ["first", "second"]
    assert conv.take_notes() == ["first", "second"] and conv.take_notes() == []


# ----------------------------------------------------------------------------- rounds


def test_round_spans_cover_complete_rounds_only():
    messages = story()
    spans = round_spans(messages, 1)
    # [user d1, assistant c1, tool c1] [user d2, assistant c2+c3, tool, tool] [assistant c4, tool] ; the tail is no round
    assert spans == [(1, 4), (4, 8), (8, 10)]
    assert round_spans(messages[:1], 1) == []
    assert round_spans([user("only unanswered")], 0) == []
    assert round_spans([], 0) == []


def test_every_round_span_keeps_tool_calls_paired():
    messages = story()
    for begin, end in round_spans(messages, 1):
        assert_paired(messages[begin:end])


# ---------------------------------------------------------------------------- trimming


def test_drop_oldest_rounds_removes_whole_rounds_from_the_front_and_keeps_the_pinned_first_message():
    conv = make(story())
    first_round = sum(message_chars(m) for m in conv.messages[1:4])
    rounds, dropped = conv.drop_oldest_rounds(first_round)
    assert rounds == 1 and dropped == first_round
    assert conv.messages[0] == user("FULL") and conv.messages[1] == user("d2")  # the opening is pinned
    assert conv.epoch == 2  # the request prefix changed
    assert_paired(conv.messages)


def test_drop_oldest_rounds_drops_as_many_rounds_as_needed():
    conv = make(story())
    rounds, dropped = conv.drop_oldest_rounds(10_000)  # more than there is
    assert rounds == 2  # the newest round is never touched
    assert [m.get("role") for m in conv.messages] == ["user", "assistant", "tool", "user"]
    assert conv.messages[1]["tool_calls"][0]["id"] == "c4" and conv.messages[-1] == user("tail-unanswered")
    assert dropped > 0 and conv.epoch == 2
    assert_paired(conv.messages)


def test_nothing_droppable_changes_nothing():
    conv = make([user("FULL"), assistant(["c1"]), tool("c1"), user("tail")])
    before = json.dumps(conv.messages)
    assert conv.drop_oldest_rounds(10_000) == (0, 0)
    assert json.dumps(conv.messages) == before and conv.epoch == 1  # a single round is the newest: it stays


def test_without_pinning_the_first_message_can_go_too():
    conv = make([user("FULL"), assistant(["c1"]), tool("c1"), assistant(["c2"]), tool("c2")])
    rounds, _ = conv.drop_oldest_rounds(1, keep_first=False)
    assert rounds == 1 and conv.messages[0]["tool_calls"][0]["id"] == "c2"
