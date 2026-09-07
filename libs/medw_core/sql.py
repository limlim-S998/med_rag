# Azure SQL Database - the relational and audit half.
#
# Why a second store at all (docs/adr/0004): the audit trail is the one thing
# in this system a regulator might read. It wants foreign keys, it wants
# constraints that make an invalid row impossible rather than unlikely, and it
# wants ad-hoc joins six months later ("every section drafted from Table
# 14.3.2.1 across all studies"). That is a relational question. Cosmos would
# answer it slowly and without integrity guarantees.
#
# Auth is the same story as everywhere else: no password. You obtain an AAD
# access token for the database scope and hand it to the driver, and the AAD
# principal is a contained user in the database. The struct-packing below is
# the one genuinely ugly part of Azure SQL + AAD, and it is ugly in every
# language, not just this one.

import struct

from azure.identity.aio import DefaultAzureCredential
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from medw_core.settings import Settings

SQL_SCOPE = "https://database.windows.net/.default"
SQL_COPT_SS_ACCESS_TOKEN = 1256   # ODBC connection attribute, from msodbcsql.h


async def access_token_struct(cred: DefaultAzureCredential) -> bytes:
    # The driver wants the token as UTF-16-LE bytes prefixed with a 4-byte
    # length. Not a string, not base64.
    token = (await cred.get_token(SQL_SCOPE)).token.encode("utf-16-le")
    return struct.pack("<I", len(token)) + token


def engine(s: Settings, token: bytes) -> AsyncEngine:
    if not s.sql_server:
        # An empty host silently produces "mssql+aioodbc://@/medw", which is a
        # valid URL that connects to nothing - so the failure surfaces later,
        # somewhere unrelated, as a driver error. Say what is actually wrong.
        raise RuntimeError(
            "MEDW_SQL_SERVER is not set. Azure SQL is not provisioned in this "
            "environment; see RESUME.md. The audit sink is unavailable until it is."
        )
    return create_async_engine(
        f"mssql+aioodbc://@{s.sql_server}/{s.sql_database}"
        "?driver=ODBC+Driver+18+for+SQL+Server",
        connect_args={"attrs_before": {SQL_COPT_SS_ACCESS_TOKEN: token}},
        pool_pre_ping=True,   # tokens expire and pooled connections go stale
    )


# --- what the audit write looks like -------------------------------------
#
# INSERT is the only verb this service has on the audit tables. No UPDATE, no
# DELETE, enforced by the role grant in db/sql/0002_audit.sql rather than by
# convention. An append-only table you can only append to is a much easier
# thing to defend than one you promise not to modify.
#
# Every generated section lands here with: who asked, which prompt bundle SHA,
# which chat deployment and model version, which chunk IDs it drew on, and the
# verification verdict. That row is the answer to "why does this paragraph in
# the submission say what it says".

AUDIT_INSERT = """
INSERT INTO audit.generation_event
    (event_id, correlation_id, study_id, section_path, user_oid,
     chat_deployment, prompt_bundle_sha, embed_version,
     source_chunk_ids, numeric_check_passed, structural_check_passed, created_at)
VALUES (:event_id, :correlation_id, :study_id, :section_path, :user_oid,
        :chat_deployment, :prompt_bundle_sha, :embed_version,
        :source_chunk_ids, :numeric_ok, :structural_ok, SYSUTCDATETIME())
"""


async def record_generation(eng: AsyncEngine, event: dict) -> None:
    ...
