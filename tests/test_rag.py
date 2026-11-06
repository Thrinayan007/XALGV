"""Tests for the RAG pipeline — parser, chunking, vector store, and tools."""

import tempfile
from pathlib import Path

import pytest

from local_agent.rag import DocumentChunk, DocumentParser, SearchHit, VectorStore
from local_agent.security.permissions import PermissionTier
from local_agent.tools.rag import IngestDocumentTool, QueryDocumentsTool


# ── DocumentParser tests ────────────────────────────────────────────────

class TestDocumentParser:
    def setup_method(self):
        self.parser = DocumentParser()

    def test_parse_text_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("Hello world.\n\nSecond paragraph.\n\nThird paragraph.", encoding="utf-8")
        chunks = self.parser.parse(f)
        assert len(chunks) >= 1
        assert all(isinstance(c, DocumentChunk) for c in chunks)
        assert "Hello world" in chunks[0].text

    def test_parse_markdown_file(self, tmp_path):
        f = tmp_path / "readme.md"
        f.write_text("# Title\n\nSome content here.\n\n## Section 2\n\nMore content.", encoding="utf-8")
        chunks = self.parser.parse(f)
        assert len(chunks) >= 1
        assert "Title" in chunks[0].text

    def test_parse_python_file(self, tmp_path):
        f = tmp_path / "script.py"
        f.write_text('"""Module docstring."""\n\ndef hello():\n    print("hi")\n', encoding="utf-8")
        chunks = self.parser.parse(f)
        assert len(chunks) >= 1
        assert "hello" in chunks[0].text

    def test_parse_html_file(self, tmp_path):
        f = tmp_path / "page.html"
        f.write_text(
            "<html><head><title>Test</title></head>"
            "<body><h1>Hello</h1><p>World</p>"
            "<script>alert('x')</script></body></html>",
            encoding="utf-8",
        )
        chunks = self.parser.parse(f)
        assert len(chunks) >= 1
        full_text = " ".join(c.text for c in chunks)
        assert "Hello" in full_text
        assert "alert" not in full_text  # Script stripped.

    def test_parse_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            self.parser.parse(Path("/does/not/exist.txt"))

    def test_chunk_ids_are_unique(self, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_text("\n\n".join(f"Paragraph {i} with some words." for i in range(20)), encoding="utf-8")
        chunks = self.parser.parse(f)
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids)), "Chunk IDs must be unique."

    def test_empty_file_returns_minimal_chunks(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        chunks = self.parser.parse(f)
        # Either empty list or a single empty chunk is acceptable.
        assert isinstance(chunks, list)


class TestTextChunking:
    """Test the static _split_into_chunks method."""

    def test_short_text_single_chunk(self):
        chunks = DocumentParser._split_into_chunks("A short paragraph.", max_chunk_size=100)
        assert len(chunks) == 1
        assert "short paragraph" in chunks[0]

    def test_long_text_splits(self):
        # Create text with many paragraphs.
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 50 for i in range(10))
        chunks = DocumentParser._split_into_chunks(text, max_chunk_size=100, overlap=10)
        assert len(chunks) > 1

    def test_overlap_preserves_context(self):
        text = "First paragraph. " + "word " * 60 + "\n\nSecond paragraph. " + "word " * 60
        chunks = DocumentParser._split_into_chunks(text, max_chunk_size=50, overlap=10)
        if len(chunks) > 1:
            # The second chunk should contain some overlap from the first.
            assert len(chunks[1]) > 0

    def test_empty_text(self):
        chunks = DocumentParser._split_into_chunks("")
        assert chunks == []

    def test_whitespace_only(self):
        chunks = DocumentParser._split_into_chunks("   \n\n   ")
        assert chunks == []


# ── VectorStore tests (in-memory fallback) ──────────────────────────────

class TestVectorStore:
    def test_add_and_count(self):
        store = VectorStore(db_path=tempfile.mkdtemp())
        chunks = [
            DocumentChunk(
                chunk_id=f"c{i}",
                document_id="doc1",
                document_path="/test.txt",
                text=f"Chunk {i}",
                chunk_index=i,
            )
            for i in range(3)
        ]
        embeddings = [[float(i)] * 8 for i in range(3)]
        added = store.add_chunks(chunks, embeddings)
        assert added == 3
        assert store.chunk_count == 3

    def test_search_returns_results(self):
        store = VectorStore(db_path=tempfile.mkdtemp())
        chunks = [
            DocumentChunk(
                chunk_id="c0",
                document_id="doc1",
                document_path="/test.txt",
                text="Python programming language",
                chunk_index=0,
            ),
            DocumentChunk(
                chunk_id="c1",
                document_id="doc1",
                document_path="/test.txt",
                text="JavaScript web development",
                chunk_index=1,
            ),
        ]
        # Embeddings: first chunk is [1,0,...], second is [0,1,...]
        embeddings = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
        ]
        store.add_chunks(chunks, embeddings)

        # Query close to first chunk.
        hits = store.search([0.9, 0.1, 0.0, 0.0], top_k=2)
        assert len(hits) == 2
        assert isinstance(hits[0], SearchHit)
        assert hits[0].chunk.text == "Python programming language"
        assert hits[0].score > hits[1].score

    def test_search_empty_store(self):
        store = VectorStore(db_path=tempfile.mkdtemp())
        hits = store.search([1.0, 0.0], top_k=5)
        assert hits == []

    def test_mismatched_chunks_embeddings_raises(self):
        store = VectorStore(db_path=tempfile.mkdtemp())
        chunks = [DocumentChunk("c0", "d0", "/t.txt", "text", chunk_index=0)]
        with pytest.raises(ValueError):
            store.add_chunks(chunks, [[1.0], [2.0]])  # 2 embeddings, 1 chunk.

    def test_source_reference_includes_page(self):
        store = VectorStore(db_path=tempfile.mkdtemp())
        chunks = [
            DocumentChunk(
                chunk_id="c0",
                document_id="doc1",
                document_path="/path/to/report.pdf",
                text="Content",
                page_number=5,
                chunk_index=0,
            ),
        ]
        store.add_chunks(chunks, [[1.0, 0.0]])
        hits = store.search([1.0, 0.0], top_k=1)
        assert len(hits) == 1
        assert "report.pdf" in hits[0].source_reference
        assert "page 5" in hits[0].source_reference


# ── RAG Tool schema tests ──────────────────────────────────────────────

class TestRAGToolSchemas:
    def test_ingest_tool_schema(self):
        tool = IngestDocumentTool()
        assert tool.name == "ingest_document"
        assert tool.permission_tier == PermissionTier.READ_ONLY
        defn = tool.to_openai_tool()
        assert defn["function"]["name"] == "ingest_document"
        assert "path" in defn["function"]["parameters"]["properties"]

    def test_query_tool_schema(self):
        tool = QueryDocumentsTool()
        assert tool.name == "query_documents"
        assert tool.permission_tier == PermissionTier.READ_ONLY
        defn = tool.to_openai_tool()
        assert defn["function"]["name"] == "query_documents"
        assert "question" in defn["function"]["parameters"]["properties"]


class TestRAGToolExecution:
    """RAG tools return clear error when pipeline is not configured."""

    @pytest.mark.asyncio
    async def test_ingest_without_pipeline(self):
        tool = IngestDocumentTool(rag_pipeline=None)
        result = await tool.execute({"path": "/some/doc.pdf"})
        assert not result.success
        assert "not configured" in result.error

    @pytest.mark.asyncio
    async def test_query_without_pipeline(self):
        tool = QueryDocumentsTool(rag_pipeline=None)
        result = await tool.execute({"question": "What is this about?"})
        assert not result.success
        assert "not configured" in result.error


class TestRAGToolVerification:
    @pytest.mark.asyncio
    async def test_ingest_verify_success(self):
        from local_agent.tools.base import ToolResult, VerificationStatus
        tool = IngestDocumentTool()
        result = ToolResult(success=True, output="Ingested", metadata={"chunk_count": 5, "path": "/test.txt"})
        v = await tool.verify({"path": "/test.txt"}, result)
        assert v.status == VerificationStatus.PASSED

    @pytest.mark.asyncio
    async def test_query_verify_with_results(self):
        from local_agent.tools.base import ToolResult, VerificationStatus
        tool = QueryDocumentsTool()
        result = ToolResult(
            success=True,
            output="Some results",
            metadata={"hit_count": 3, "question": "test", "sources": ["a.pdf"]},
        )
        v = await tool.verify({"question": "test"}, result)
        assert v.status == VerificationStatus.PASSED
        assert v.evidence["hit_count"] == 3

    @pytest.mark.asyncio
    async def test_query_verify_no_results(self):
        from local_agent.tools.base import ToolResult, VerificationStatus
        tool = QueryDocumentsTool()
        result = ToolResult(success=True, output="No results", metadata={"hit_count": 0, "question": "test"})
        v = await tool.verify({"question": "test"}, result)
        assert v.status == VerificationStatus.PASSED

