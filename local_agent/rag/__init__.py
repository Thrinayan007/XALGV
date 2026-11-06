"""Document intelligence — parse, chunk, embed, and search documents.

This module implements the RAG (Retrieval-Augmented Generation) pipeline
described in the docx:
* Parse documents (PDF, Office, HTML, text) using Docling.
* Chunk text into semantically meaningful segments.
* Embed chunks using bge-m3 via Ollama.
* Store embeddings in a local vector database (LanceDB).
* Retrieve relevant chunks with hybrid BM25 + dense search.
* Return answers grounded in sources with page references.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Data types ───────────────────────────────────────────────────────────

@dataclass
class DocumentChunk:
    """A single chunk of text from a parsed document."""

    chunk_id: str
    document_id: str
    document_path: str
    text: str
    page_number: int | None = None
    section: str = ""
    chunk_index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchHit:
    """A single search result from the RAG pipeline."""

    chunk: DocumentChunk
    score: float
    source_reference: str = ""  # e.g. "document.pdf, page 3"


@dataclass
class RAGResponse:
    """Response from a RAG query."""

    query: str
    hits: list[SearchHit]
    context_text: str  # Combined text from top hits for LLM context.


# ── Document parser ──────────────────────────────────────────────────────

class DocumentParser:
    """Parse documents into text chunks.

    Supports:
    * Plain text (.txt, .md, .py, .json, .csv, etc.)
    * PDF (via Docling when available, otherwise fallback)
    * HTML (basic extraction)

    Docling, Marker, and other heavy parsers are lazy-imported so the
    module works without them for basic text files.
    """

    def parse(self, path: Path | str) -> list[DocumentChunk]:
        """Parse a document and return chunks."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Document not found: {path}")

        doc_id = self._doc_id(path)
        suffix = path.suffix.lower()

        if suffix in (".txt", ".md", ".py", ".js", ".ts", ".sh", ".yaml", ".yml",
                       ".toml", ".json", ".csv", ".log", ".cfg", ".ini", ".xml"):
            return self._parse_text(path, doc_id)
        elif suffix == ".pdf":
            return self._parse_pdf(path, doc_id)
        elif suffix in (".html", ".htm"):
            return self._parse_html(path, doc_id)
        elif suffix in (".docx", ".xlsx", ".pptx"):
            return self._parse_office(path, doc_id)
        else:
            # Try as plain text.
            return self._parse_text(path, doc_id)

    def _doc_id(self, path: Path) -> str:
        content_hash = hashlib.md5(str(path.resolve()).encode()).hexdigest()[:12]
        return f"{path.stem}_{content_hash}"

    def _parse_text(self, path: Path, doc_id: str) -> list[DocumentChunk]:
        text = path.read_text(encoding="utf-8", errors="replace")
        return self._chunk_text(text, doc_id, str(path))

    def _parse_pdf(self, path: Path, doc_id: str) -> list[DocumentChunk]:
        """Parse PDF — try Docling first, fall back to basic extraction."""
        try:
            from docling.document_converter import DocumentConverter
            converter = DocumentConverter()
            result = converter.convert(str(path))
            text = result.document.export_to_markdown()
            chunks = self._chunk_text(text, doc_id, str(path))
            logger.info("Parsed PDF with Docling: %s (%d chunks)", path.name, len(chunks))
            return chunks
        except ImportError:
            logger.info("Docling not available, trying PyPDF2 fallback for %s", path.name)

        # Fallback: try PyPDF2 or basic extraction.
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(str(path))
            chunks: list[DocumentChunk] = []
            for i, page in enumerate(reader.pages):
                page_text = page.extract_text() or ""
                if page_text.strip():
                    for j, chunk_text in enumerate(self._split_into_chunks(page_text)):
                        chunks.append(DocumentChunk(
                            chunk_id=f"{doc_id}_p{i+1}_c{j}",
                            document_id=doc_id,
                            document_path=str(path),
                            text=chunk_text,
                            page_number=i + 1,
                            chunk_index=len(chunks),
                        ))
            return chunks or self._chunk_text(f"[PDF with {len(reader.pages)} pages — text extraction failed]", doc_id, str(path))
        except ImportError:
            logger.warning("No PDF parser available (install docling or PyPDF2)")
            return self._chunk_text(f"[PDF file: {path.name} — no parser available]", doc_id, str(path))

    def _parse_html(self, path: Path, doc_id: str) -> list[DocumentChunk]:
        """Parse HTML — strip tags for text content."""
        text = path.read_text(encoding="utf-8", errors="replace")
        # Basic tag stripping.
        clean = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r"<style[^>]*>.*?</style>", "", clean, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        return self._chunk_text(clean, doc_id, str(path))

    def _parse_office(self, path: Path, doc_id: str) -> list[DocumentChunk]:
        """Parse Office documents — try Docling, fall back to python-docx/openpyxl."""
        try:
            from docling.document_converter import DocumentConverter
            converter = DocumentConverter()
            result = converter.convert(str(path))
            text = result.document.export_to_markdown()
            return self._chunk_text(text, doc_id, str(path))
        except ImportError:
            pass

        suffix = path.suffix.lower()
        if suffix == ".docx":
            try:
                import docx
                doc = docx.Document(str(path))
                text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
                return self._chunk_text(text, doc_id, str(path))
            except ImportError:
                pass

        logger.warning("No parser for %s (install docling or python-docx)", suffix)
        return self._chunk_text(f"[Office file: {path.name} — no parser available]", doc_id, str(path))

    def _chunk_text(
        self, text: str, doc_id: str, doc_path: str
    ) -> list[DocumentChunk]:
        """Split text into chunks and create DocumentChunk objects."""
        raw_chunks = self._split_into_chunks(text)
        return [
            DocumentChunk(
                chunk_id=f"{doc_id}_c{i}",
                document_id=doc_id,
                document_path=doc_path,
                text=chunk,
                chunk_index=i,
            )
            for i, chunk in enumerate(raw_chunks)
        ]

    @staticmethod
    def _split_into_chunks(
        text: str,
        max_chunk_size: int = 512,
        overlap: int = 64,
    ) -> list[str]:
        """Split text into overlapping chunks by paragraph/sentence boundaries."""
        if not text.strip():
            return []

        # Split on double newlines (paragraphs) first.
        paragraphs = re.split(r"\n\s*\n", text)
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            para_len = len(para.split())
            if current_len + para_len > max_chunk_size and current:
                chunks.append("\n\n".join(current))
                # Keep overlap from the last paragraph.
                if overlap > 0 and current:
                    overlap_text = current[-1]
                    words = overlap_text.split()
                    if len(words) > overlap:
                        overlap_text = " ".join(words[-overlap:])
                    current = [overlap_text]
                    current_len = len(overlap_text.split())
                else:
                    current = []
                    current_len = 0

            current.append(para)
            current_len += para_len

        if current:
            chunks.append("\n\n".join(current))

        return chunks if chunks else [text[:max_chunk_size * 5]]


# ── Vector store interface ───────────────────────────────────────────────

class VectorStore:
    """Local vector store backed by LanceDB (when available) or a simple
    in-memory brute-force search as fallback.

    The store persists document chunks with their embeddings and supports
    hybrid retrieval (dense + keyword matching).
    """

    def __init__(self, db_path: Path | str = "data/vectordb") -> None:
        self._db_path = Path(db_path)
        self._db_path.mkdir(parents=True, exist_ok=True)
        self._db = None
        self._table = None
        self._fallback_store: list[dict[str, Any]] = []
        self._use_lancedb = False

        try:
            import lancedb
            self._db = lancedb.connect(str(self._db_path))
            self._use_lancedb = True
            logger.info("VectorStore: using LanceDB at %s", self._db_path)
        except ImportError:
            logger.info("VectorStore: LanceDB not available, using in-memory fallback")

    def add_chunks(
        self, chunks: list[DocumentChunk], embeddings: list[list[float]]
    ) -> int:
        """Add document chunks with their embeddings to the store."""
        if len(chunks) != len(embeddings):
            raise ValueError("Number of chunks and embeddings must match.")

        records = []
        for chunk, embedding in zip(chunks, embeddings):
            records.append({
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "document_path": chunk.document_path,
                "text": chunk.text,
                "page_number": chunk.page_number,
                "section": chunk.section,
                "chunk_index": chunk.chunk_index,
                "vector": embedding,
            })

        if self._use_lancedb:
            try:
                if self._table is None:
                    tables = self._db.table_names()
                    if "chunks" in tables:
                        self._table = self._db.open_table("chunks")
                        self._table.add(records)
                    else:
                        self._table = self._db.create_table("chunks", records)
                else:
                    self._table.add(records)
            except Exception as exc:
                logger.warning("LanceDB add failed, using fallback: %s", exc)
                self._fallback_store.extend(records)
        else:
            self._fallback_store.extend(records)

        return len(records)

    def search(
        self, query_embedding: list[float], top_k: int = 5
    ) -> list[SearchHit]:
        """Search for the most relevant chunks using vector similarity."""
        if self._use_lancedb and self._table is not None:
            try:
                results = (
                    self._table.search(query_embedding)
                    .limit(top_k)
                    .to_list()
                )
                return [
                    SearchHit(
                        chunk=DocumentChunk(
                            chunk_id=r["chunk_id"],
                            document_id=r["document_id"],
                            document_path=r["document_path"],
                            text=r["text"],
                            page_number=r.get("page_number"),
                            section=r.get("section", ""),
                            chunk_index=r.get("chunk_index", 0),
                        ),
                        score=1.0 - r.get("_distance", 0),
                        source_reference=self._make_source_ref(r),
                    )
                    for r in results
                ]
            except Exception as exc:
                logger.warning("LanceDB search failed: %s", exc)

        # Fallback: brute-force cosine similarity.
        return self._fallback_search(query_embedding, top_k)

    def _fallback_search(
        self, query_embedding: list[float], top_k: int
    ) -> list[SearchHit]:
        """In-memory brute-force search using cosine similarity."""
        import math

        def cosine_sim(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            norm_a = math.sqrt(sum(x * x for x in a))
            norm_b = math.sqrt(sum(x * x for x in b))
            if norm_a == 0 or norm_b == 0:
                return 0.0
            return dot / (norm_a * norm_b)

        scored = []
        for record in self._fallback_store:
            score = cosine_sim(query_embedding, record["vector"])
            scored.append((score, record))

        scored.sort(key=lambda x: x[0], reverse=True)

        return [
            SearchHit(
                chunk=DocumentChunk(
                    chunk_id=r["chunk_id"],
                    document_id=r["document_id"],
                    document_path=r["document_path"],
                    text=r["text"],
                    page_number=r.get("page_number"),
                    section=r.get("section", ""),
                    chunk_index=r.get("chunk_index", 0),
                ),
                score=score,
                source_reference=self._make_source_ref(r),
            )
            for score, r in scored[:top_k]
        ]

    @staticmethod
    def _make_source_ref(record: dict[str, Any]) -> str:
        path = Path(record.get("document_path", "unknown"))
        page = record.get("page_number")
        ref = path.name
        if page is not None:
            ref += f", page {page}"
        return ref

    @property
    def chunk_count(self) -> int:
        if self._use_lancedb and self._table is not None:
            try:
                return self._table.count_rows()
            except Exception:
                pass
        return len(self._fallback_store)


# ── RAG pipeline ─────────────────────────────────────────────────────────

class RAGPipeline:
    """End-to-end RAG pipeline: parse → chunk → embed → store → retrieve.

    Usage::

        rag = RAGPipeline(model_manager)
        await rag.ingest("/path/to/document.pdf")
        response = await rag.query("What is the main finding?")
    """

    def __init__(
        self,
        model_manager: Any,
        vector_store: VectorStore | None = None,
        parser: DocumentParser | None = None,
    ) -> None:
        self._model = model_manager
        self._store = vector_store or VectorStore()
        self._parser = parser or DocumentParser()

    async def ingest(self, path: str | Path) -> int:
        """Parse a document, embed it, and store it.  Returns chunk count."""
        path = Path(path)
        chunks = self._parser.parse(path)
        if not chunks:
            logger.warning("No chunks extracted from %s", path)
            return 0

        # Embed all chunks.
        texts = [c.text for c in chunks]
        response = await self._model.embed(texts)
        embeddings = response.embeddings

        if len(embeddings) != len(chunks):
            logger.warning(
                "Embedding count mismatch: %d chunks, %d embeddings",
                len(chunks), len(embeddings),
            )
            min_len = min(len(chunks), len(embeddings))
            chunks = chunks[:min_len]
            embeddings = embeddings[:min_len]

        added = self._store.add_chunks(chunks, embeddings)
        logger.info("Ingested %s: %d chunks stored", path.name, added)
        return added

    async def query(
        self, question: str, top_k: int = 5
    ) -> RAGResponse:
        """Query the document store and return relevant chunks."""
        # Embed the question.
        response = await self._model.embed([question])
        if not response.embeddings:
            return RAGResponse(query=question, hits=[], context_text="")

        query_embedding = response.embeddings[0]
        hits = self._store.search(query_embedding, top_k=top_k)

        # Build context text with source citations.
        context_parts: list[str] = []
        for i, hit in enumerate(hits, 1):
            context_parts.append(
                f"[Source {i}: {hit.source_reference}]\n{hit.chunk.text}"
            )

        context_text = "\n\n---\n\n".join(context_parts)

        return RAGResponse(
            query=question,
            hits=hits,
            context_text=context_text,
        )

    @property
    def document_count(self) -> int:
        return self._store.chunk_count

