"""Persist the exact source/index selection and effective generation identity."""

from medw_core.audit_events import generation_event
from medw_core.ports import AuditSink
from medw_core.provenance import Provenance
from medw_core.schemas import Citation, IndexGeneration


async def record(sink: AuditSink, prov: Provenance, *, generation: IndexGeneration,
                 correlation_id: str, section_path: str, user_oid: str,
                 citations: list[Citation], output_text: str, numeric_ok: bool,
                 structural_ok: bool) -> str:
    return await sink.record_generation(generation_event(
        prov, generation, citations=citations, correlation_id=correlation_id,
        section_path=section_path, user_oid=user_oid, output_text=output_text,
        numeric_ok=numeric_ok, structural_ok=structural_ok))
