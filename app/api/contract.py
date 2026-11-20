"""The official contract API (v0.14; docs/v0.12-contracts.md §13.7, the "files" table; H38).

    GET  /api/contract            the contract in force (``?download=1``: the file, ``contract-<version>.md``)
    PUT  /api/contract            {markdown}: replace it (checked first: 400 with every problem)
    POST /api/contract:reset      bring the default contract back

A change makes every bot's initialization ``stale`` (the hash of the contract changed) - nothing has to be told to the bots - and the
orchestrator is told (``orchestrator.sources_changed("contract", None)``, when it has such a method) so that it can refresh what it shows.
Reading needs the API token when there is one; changing also needs a trusted client (loopback, or the token).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api._common import LazyOrchestrator, attachment, error_response, maybe_await, problems_detail, safe_filename
from app.api.security import require_trusted_client, verify_api_access
from app.storage.contract import ContractError, ContractStore, get_contract_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_access)])
TRUSTED = [Depends(require_trusted_client)]

orchestrator: Any = LazyOrchestrator()


class ContractUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str = Field(max_length=2_000_000)


def contract_payload(store: ContractStore) -> dict[str, Any]:
    """``{"markdown", "source": {"origin", "version", "imported_at", "sha256"}, "chars", "must_read_chars", "required_terms": [...],
    "problems": [...]}`` - ``problems`` (an addition) is what is wrong with the contract in the data directory right now (edited by hand
    or left over from an older program); ``[]`` when it is fine."""
    from app.agents.contract import required_terms

    doc = store.current()
    problems = store.validate(doc.markdown) if doc.markdown.strip() else ["官方契约是空的，请导入契约或恢复默认。"]
    return {
        "markdown": doc.markdown,
        "source": store.source(),
        "chars": len(doc.markdown),
        "must_read_chars": len(doc.must_read),
        "required_terms": required_terms(),
        "problems": problems,
    }


async def _notify(kind: str, scenario_id: str | None = None) -> None:
    """Tell the orchestrator that the contract / a scenario / its materials changed (it rebuilds an idle game and says
    ``bots.updated``); nothing happens when it has no ``sources_changed`` hook (yet).  A hook that fails never fails the request."""
    try:
        hook = getattr(orchestrator, "sources_changed", None)
    except Exception:  # noqa: BLE001 - no orchestrator at all (a store test)
        return
    if callable(hook):
        try:
            await maybe_await(hook(kind, scenario_id))
        except Exception:  # noqa: BLE001
            logger.warning("sources_changed failed after a change of the %s", kind, exc_info=True)


@router.get("/contract")
async def get_contract(download: bool = Query(default=False)) -> Any:
    store = get_contract_store()
    if download:
        text = (await asyncio.to_thread(store.current)).markdown
        if not text.strip():
            return error_response(404, "官方契约是空的，没有可以下载的文件。", code="not_found")
        label = await asyncio.to_thread(store.download_label)
        return attachment(text, f"contract-{safe_filename(label, 'custom')}.md")
    return await asyncio.to_thread(contract_payload, store)


@router.put("/contract", dependencies=TRUSTED)
async def put_contract(req: ContractUpdate) -> Any:
    """Replace the contract.  Refused with every problem (400 ``problems``: the front matter, the two parts, the length of ``必读``, each
    tool and receipt code it does not mention) and nothing written; accepted: the answer is the new contract, every bot is ``stale``."""
    store = get_contract_store()
    try:
        await asyncio.to_thread(store.set, req.markdown)
    except ContractError as exc:
        return error_response(exc.status, problems_detail("契约没有保存：", exc.problems), code=exc.code, problems=exc.problems)
    await _notify("contract")
    return await asyncio.to_thread(contract_payload, store)


@router.post("/contract:reset", dependencies=TRUSTED)
async def reset_contract() -> Any:
    store = get_contract_store()
    try:
        await asyncio.to_thread(store.reset)
    except ContractError as exc:
        return error_response(exc.status, problems_detail("", exc.problems), code=exc.code, problems=exc.problems)
    await _notify("contract")
    return await asyncio.to_thread(contract_payload, store)


__all__ = ["contract_payload", "router"]
