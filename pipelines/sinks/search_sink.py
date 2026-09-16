"""Compatibility exports; ingestion sinks are shared with the HTTP worker."""

from medw_core.search_sink import (
    SearchGenerationSink,
    delete_study,
    generation_filter,
    index_chunks,
)

__all__ = ['SearchGenerationSink', 'delete_study', 'generation_filter', 'index_chunks']
