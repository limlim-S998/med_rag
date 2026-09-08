# One completed section = one row in audit.generation_event.
#
# What makes this row worth having: it is written from the values the service
# actually used, not from config it read at startup. If a Helm value changed
# mid-rollout and half the pods are on the old prompt bundle, the rows say so.
# A row that reports intended configuration instead of effective configuration
# is worse than no row, because it is confidently wrong.
#
# Written after the stream completes and verification finishes, in one INSERT.
# The fat payload (full prose, slots, the judgement JSON) goes to the Cosmos
# `generations` container with a 90-day TTL; the joinable, permanent row goes
# here. Same reason the two stores exist at all.

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from medw_core.provenance import Provenance
from medw_core.sql import AUDIT_INSERT


async def record(eng: AsyncEngine, prov: Provenance, *, correlation_id: str,
                 study_id: str, section_path: str, user_oid: str,
                 source_chunk_ids: list[str], numeric_ok: bool,
                 structural_ok: bool) -> str:
    """Takes a Provenance, not Settings.

    The difference matters: Settings is what this process was configured with
    and is mutable in principle; Provenance is a frozen snapshot captured at
    startup. Passing Settings here would let the version columns be read at
    write time, which during a rolling deploy is a different answer from the
    one that actually produced the text.

    It also means adding a fourth version axis touches medw_core.provenance
    and nothing else - previously it would have meant editing this call site,
    the metric dimensions and every log record independently.
    """
    event_id = str(uuid.uuid4())
    async with eng.begin() as conn:
        await conn.execute(text(AUDIT_INSERT), {
            "event_id": event_id,
            "correlation_id": correlation_id,
            "study_id": study_id,
            "section_path": section_path,
            "user_oid": user_oid,
            **prov.as_dict(),
            "source_chunk_ids": '["' + '","'.join(source_chunk_ids) + '"]',
            "numeric_ok": numeric_ok,
            "structural_ok": structural_ok,
        })
    return event_id


# A numeric-fidelity failure fails the section - it does not warn, and it is
# still audited. The row for a rejected draft is as interesting as the row for
# an accepted one: a rise in failures against one deployment name is the
# earliest signal that a model version moved underneath you.
