"""The question bank, as code: every file below ``bench/fixtures`` is written by a ``Case`` of this package (``python -m bench fixtures``).

One module per suite (``l1`` ... ``s2``); ``all_cases()`` lists them.  The code is the source of the files - and a test checks that the committed files are
exactly what the code writes - so a question is changed here and regenerated, never edited in place.  What a question *tests* and where its *trap* is
stays in the comment at the top of its file.
"""

from __future__ import annotations

from bench.cases.common import Case


def all_cases() -> list[Case]:
    """Every case of the bank, in suite order."""
    from bench.cases import l1, l2, l3, l4, m2, m3, r, s2  # (late: each module builds its worlds when asked, not when imported)

    cases: list[Case] = []
    for module in (r, s2, l1, l2, l3, l4, m2, m3):
        cases.extend(module.cases())
    ids = [c.path for c in cases]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise ValueError(f"题库里有重复的文件：{duplicated}")
    return cases


__all__ = ["Case", "all_cases"]
