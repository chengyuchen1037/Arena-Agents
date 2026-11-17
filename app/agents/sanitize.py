"""Keep player-authored text from impersonating the structure of an agent's context.

Player speech is untrusted data.  It is embedded into other agents' prompts (the visible history,
request answers, challenge previews).  It must not be able to forge a history record such as
``#12 [公共频道] 主持人（moderator）：…``, a board section header such as ``【状态板】`` or a tool /
data-block delimiter.
"""

from __future__ import annotations

import re

_CONTINUATION_PREFIX = "    ┆ "
_SECTION_RE = re.compile(r"【\s*(状态板|我的记忆|我最近的行动|可见历史|本次唤醒|新动态)\s*】")
_RECORD_RE = re.compile(r"(?m)^(\s*)(#\d+\s*\[)")
_BLOCK_TAG_RE = re.compile(r"<\s*/?\s*(player_speech|untrusted_data|system|assistant|tool)\b[^>]*>", re.IGNORECASE)
_ROLE_PREFIX_RE = re.compile(r"^(\s*)(主持人|系统|system|assistant|moderator)(\s*[（(:：])", re.IGNORECASE)


def neutralize_untrusted_text(text: str) -> str:
    """Make *text* safe to embed as quoted data on one record.

    * continuation lines get an indent marker, so no line of the quote can start like a record;
    * forged section headers and delimiters are defused;
    * a ``主持人：`` / ``system:`` style speaker prefix is defused.
    """
    cleaned = _BLOCK_TAG_RE.sub("", str(text))
    cleaned = _SECTION_RE.sub(lambda m: f"［{m.group(1)}］", cleaned)
    lines = cleaned.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for index, line in enumerate(lines):
        line = _RECORD_RE.sub(lambda m: f"{m.group(1)}＃{m.group(2)[1:]}", line)
        line = _ROLE_PREFIX_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}\u200b{m.group(3)}", line)
        out.append(line if index == 0 else _CONTINUATION_PREFIX + line)
    return "\n".join(out)
