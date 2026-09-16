"""Compatibility exports; ingestion sinks are shared with the HTTP worker."""

from medw_core.qdrant_sink import QdrantGenerationSink, swap_alias, upsert_chunks

__all__ = ['QdrantGenerationSink', 'swap_alias', 'upsert_chunks']
