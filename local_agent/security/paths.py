"""Protect browser authentication profiles from document ingestion."""

from pathlib import Path


def document_path(path):
    original = Path(path)
    resolved = original.resolve()
    if any(part.casefold() == ".xbrowser" for part in (*original.parts, *resolved.parts)):
        raise PermissionError("Browser profiles cannot be ingested as documents")
    return resolved
