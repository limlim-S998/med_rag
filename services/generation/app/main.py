"""Retrieve evidence, stream the installed placeholder, retain its exact audit."""

import json
import logging
import uuid
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from medw_core import tracing
from medw_core.audit_events import generation_event
from medw_core.auth import Principal, study_user
from medw_core.composition import effective_settings
from medw_core.persistence import Conflict
from medw_core.schemas import RetrievalResponse
from medw_core.service import (
    add_platform_routes,
    attach_request_instrumentation,
    lifespan_for,
    readiness_response,
)
from medw_core.settings import get_settings

from .verify import placeholder_verification

s = effective_settings(get_settings().model_copy(update={"service_name": "generation"}))
PROMPTS = Path(__file__).parent / "prompts"
app = FastAPI(title="generation", lifespan=lifespan_for("generation", s, prompts=PROMPTS))
attach_request_instrumentation(app, "generation")
add_platform_routes(app, s)
logger = logging.getLogger(__name__)


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=16000)
    top_k: int = Field(default=8, ge=1, le=20)
    max_tokens: int = Field(default=2048, ge=128, le=8192)


@app.get("/healthz")
async def healthz() -> Response:
    return Response(status_code=200)


@app.get("/readyz")
async def readyz(request: Request) -> Response:
    return await readiness_response(request)


def line(kind: str, **payload) -> str:
    return json.dumps({"type": kind, **payload}) + "\n"


@app.post("/studies/{study_id}/sections/{section_path}/draft")
async def draft(body: DraftRequest, request: Request,
                study_id: str = PathParam(max_length=32),
                section_path: str = PathParam(max_length=32),
                user: Principal = Depends(study_user)):
    services = request.app.state.services
    if services is None:
        raise HTTPException(503, "generation dependencies unavailable")
    try:
        response = await request.app.state.http.post(
            f"{s.retrieval_url}/search",
            json={"study_id": study_id, "query": body.query, "top_k": body.top_k},
        )
        if response.status_code == 409:
            raise HTTPException(409, "no compatible published index generation")
        response.raise_for_status()
        retrieved = RetrievalResponse.model_validate(response.json())
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        raise HTTPException(503, "retrieval unavailable") from exc
    generation = retrieved.index_generation
    if generation is None or generation.study_id != study_id:
        raise HTTPException(503, "retrieval returned an invalid index selection")
    citations = [hit.citation for hit in retrieved.hits if hit.citation is not None]
    if not citations:
        raise HTTPException(409, "no evidence available for this draft")
    # Validate retained citation identity before any successful response header.
    try:
        evidence_store = services.require("evidence")
        await evidence_store.validate_selection(generation, citations)
        for hit in retrieved.hits:
            if hit.citation is not None:
                retained, _, _ = await evidence_store.resolve(hit.citation)
                if retained.text != hit.text or retained.section_path != hit.section_path:
                    raise ValueError("retrieved text differs from retained source evidence")
    except (LookupError, ValueError, Conflict) as exc:
        raise HTTPException(503, "retrieval evidence unavailable") from exc
    evidence = "\n".join(
        f"[{hit.chunk_id}; source {hit.citation.source_revision}] {hit.text}"
        for hit in retrieved.hits if hit.citation is not None)
    prompt = (PROMPTS.joinpath("section_draft.md").read_text()
              + f"\nSection: {section_path}\nQuery: {body.query}\nEVIDENCE:\n" + evidence)
    event_id = str(uuid.uuid4())
    correlation_id = tracing.CORRELATION_ID.get()

    async def stream():
        yield line("start", draft_id=event_id, correlation_id=correlation_id,
                   implementation="placeholder", index_generation=generation.model_dump())
        parts: list[str] = []
        try:
            async for text in services.require("chat").stream(prompt, max_tokens=body.max_tokens):
                parts.append(text)
                yield line("delta", text=text)
            output = "".join(parts)
            verification = placeholder_verification(output)
            event = generation_event(
                request.app.state.provenance, generation, event_id=event_id,
                citations=citations, correlation_id=correlation_id,
                section_path=section_path, user_oid=user.oid, output_text=output,
                numeric_ok=False, structural_ok=False)
            await services.require("audit").record_generation(event)
            saved = await services.require("drafts").create(event)
            yield line("complete", **saved,
                       citations=[citation.model_dump() for citation in citations],
                       verification=verification, provenance=request.app.state.provenance.as_dict())
        except Exception:
            logger.exception("draft generation or persistence failed")
            # HTTP headers may have been sent. Clients must require `complete`;
            # streamed text without it is not a committed/acceptable draft.
            yield line("error", code="generation_failed", draft_id=event_id,
                       correlation_id=correlation_id)

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
