"""RAG tools — document ingestion and query as proper agent tools.

Wraps the RAG pipeline (:mod:`local_agent.rag`) into the standard tool
interface so the agent loop can ingest and query documents just like
any other tool, with permission gating and verification.
"""

from __future__ import annotations

from typing import Any

from local_agent.security.permissions import PermissionTier
from local_agent.tools.base import Tool, ToolResult, VerificationResult, VerificationStatus


class IngestDocumentTool(Tool):
    """Ingest a document into the RAG vector store for later querying.

    Parses the document (PDF, Office, HTML, text), chunks it, embeds it
    via bge-m3, and stores the vectors for semantic retrieval.
    """

    def __init__(self, rag_pipeline: Any = None) -> None:
        self._rag = rag_pipeline

    @property
    def name(self) -> str:
        return "ingest_document"

    @property
    def description(self) -> str:
        return (
            "Ingest a document file (PDF, DOCX, HTML, TXT, etc.) into the "
            "knowledge base for later querying.  The document is parsed, "
            "chunked, and embedded.  Returns the number of chunks created."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the document file to ingest.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._rag is None:
            return ToolResult(
                success=False,
                output="",
                error="RAG pipeline is not configured.",
            )

        path = params.get("path", "")
        try:
            chunk_count = await self._rag.ingest(path)
            return ToolResult(
                success=True,
                output=f"Successfully ingested '{path}': {chunk_count} chunks created.",
                metadata={"chunk_count": chunk_count, "path": path},
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                output="",
                error=f"Document not found: {path}",
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Ingestion failed: {exc}",
            )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success and result.metadata.get("chunk_count", 0) > 0:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Document ingested: {result.metadata['chunk_count']} chunks.",
                evidence=result.metadata,
            )
        if result.success:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message="Document ingested but produced no chunks.",
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"Ingestion failed: {result.error}",
        )


class QueryDocumentsTool(Tool):
    """Query the RAG knowledge base to find information from ingested documents.

    Returns the most relevant text chunks along with source references
    (file name and page number when available).
    """

    def __init__(self, rag_pipeline: Any = None) -> None:
        self._rag = rag_pipeline

    @property
    def name(self) -> str:
        return "query_documents"

    @property
    def description(self) -> str:
        return (
            "Search ingested documents for information relevant to a question.  "
            "Returns the top matching text chunks with source citations "
            "(file name and page numbers).  Use this after ingesting documents."
        )

    @property
    def permission_tier(self) -> PermissionTier:
        return PermissionTier.READ_ONLY

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to search for in the document knowledge base.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5).",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        }

    async def execute(self, params: dict[str, Any]) -> ToolResult:
        if self._rag is None:
            return ToolResult(
                success=False,
                output="",
                error="RAG pipeline is not configured.",
            )

        question = params.get("question", "")
        top_k = params.get("top_k", 5)

        try:
            response = await self._rag.query(question, top_k=top_k)

            if not response.hits:
                return ToolResult(
                    success=True,
                    output="No relevant documents found for the query.",
                    metadata={"hit_count": 0, "question": question},
                )

            # Format results with source citations.
            lines: list[str] = []
            for i, hit in enumerate(response.hits, 1):
                score_pct = f"{hit.score:.1%}" if hit.score <= 1.0 else f"{hit.score:.2f}"
                lines.append(
                    f"--- Result {i} [{hit.source_reference}] "
                    f"(relevance: {score_pct}) ---\n"
                    f"{hit.chunk.text}"
                )

            output = "\n\n".join(lines)
            return ToolResult(
                success=True,
                output=output,
                metadata={
                    "hit_count": len(response.hits),
                    "question": question,
                    "sources": [h.source_reference for h in response.hits],
                },
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=f"Query failed: {exc}",
            )

    async def verify(self, params: dict[str, Any], result: ToolResult) -> VerificationResult:
        if result.success and result.metadata.get("hit_count", 0) > 0:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message=f"Query returned {result.metadata['hit_count']} results.",
                evidence={
                    "hit_count": result.metadata["hit_count"],
                    "sources": result.metadata.get("sources", []),
                },
            )
        if result.success:
            return VerificationResult(
                status=VerificationStatus.PASSED,
                message="Query completed but found no matching documents.",
            )
        return VerificationResult(
            status=VerificationStatus.FAILED,
            message=f"Query failed: {result.error}",
        )

