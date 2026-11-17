"""config.yml hygiene: no dead keys, no duplicated parameters, sane defaults."""

from __future__ import annotations

import pytest

from app.config import BASE_DIR, load_config
from app.engine.llm import (
    DEFAULT_RETRY_AFTER_CAP,
    DEFAULT_RETRY_BASE_DELAY,
    DEFAULT_RETRY_JITTER,
    DEFAULT_RETRY_MAX_ATTEMPTS,
    DEFAULT_RETRY_MAX_DELAY,
    LLMClient,
)


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config(BASE_DIR / "config.yml")


def test_removed_dead_keys_stay_removed(cfg: dict) -> None:
    # None of these is read anywhere in app/, static/, tests/ or prompts/.
    assert "engine" not in cfg  # engine.driver had no implementation
    assert "provider" not in cfg["llm"]
    assert "extract_think_tags" not in cfg["llm"]["thinking"]
    assert "default_theme" not in cfg["app"]
    assert "interrupt_evaluation" not in cfg["prompts"]
    # v0.12: the flow is driven by the moderator agent, not by kickoff texts / an initial phase
    assert "initial_state" not in cfg
    assert "kickoff_instruction" not in cfg["prompts"] and "resume_instruction" not in cfg["prompts"]


def test_keys_that_are_still_read_are_kept(cfg: dict) -> None:
    assert cfg["app"]["title"] and cfg["app"]["version"]
    assert cfg["scenario"]["path"]
    assert cfg["knowledge_base"]["docs_dir"]
    assert cfg["prompts"]["moderator_system"] and cfg["prompts"]["participant_system"]
    runtime = cfg["runtime"]
    # v0.12.1: the engine saves nothing - no debounce, no round caps, no execution depth; concurrency is unlimited by default
    assert runtime["llm_concurrency"] == 0 and runtime["llm_concurrency_per_provider"] == 0 and runtime["watchdog"] > 0
    assert 0 < runtime["compact_at"] < runtime["trim_at"] < 1
    for gone in ("debounce", "max_rounds", "ambient_cap"):
        assert gone not in runtime
    assert "execution_depth" not in cfg["llm"]
    assert cfg["participants"] and cfg["tag_styles"]


def test_reasoning_effort_is_not_duplicated_or_hardcoded_for_the_moderator(cfg: dict) -> None:
    thinking = cfg["llm"]["thinking"]
    assert thinking["reasoning_effort"]
    assert "reasoning_effort" not in (thinking.get("extra_body") or {})
    assert "reasoning_effort" not in (cfg["llm"].get("moderator_override") or {})


def test_config_defaults_agree_with_llm_client_defaults(cfg: dict) -> None:
    llm_cfg = cfg["llm"]
    # (DEEPSEEK_BASE_URL from .env may override the host, but never adds /v1 by itself)
    assert llm_cfg["base_url"]
    assert not llm_cfg["base_url"].rstrip("/").endswith("/v1")
    assert int(llm_cfg["context_window_tokens"]) == 1_000_000
    client = LLMClient(cfg)
    assert client.context_window_tokens == 1_000_000
    assert client.max_tokens == int(llm_cfg["max_tokens"])
    assert client.timeout == float(llm_cfg["timeout_seconds"])


def test_retry_section_matches_documented_defaults(cfg: dict) -> None:
    retry = cfg["llm"]["retry"]
    assert retry["max_attempts"] == DEFAULT_RETRY_MAX_ATTEMPTS == 3
    assert retry["base_delay_seconds"] == DEFAULT_RETRY_BASE_DELAY
    assert retry["max_delay_seconds"] == DEFAULT_RETRY_MAX_DELAY
    assert retry["jitter"] == DEFAULT_RETRY_JITTER
    assert retry["max_retry_after_seconds"] == DEFAULT_RETRY_AFTER_CAP
    client = LLMClient(cfg)
    assert client.retry_max_attempts == 3
    assert client.retry_base_delay == 0.5
    assert client.retry_max_delay == 8.0
    assert client.retry_jitter == 0.25
    assert client.retry_after_cap == 60.0


def test_config_yml_effort_reaches_moderator_and_players(cfg: dict) -> None:
    client = LLMClient(cfg)
    assert client._resolve_effort(False) == cfg["llm"]["thinking"]["reasoning_effort"]
    assert client._resolve_effort(True) == cfg["llm"]["thinking"]["reasoning_effort"]
    client.reasoning_effort = "low"
    assert client._resolve_effort(True) == "low"
    assert client._resolve_effort(False) == "low"
