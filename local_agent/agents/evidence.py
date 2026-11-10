"""Structured search provenance and static Markdown checks."""

import ipaddress
import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from pydantic import Field, StringConstraints, model_validator

from .base import Contract, Identifier, Summary

_MARKDOWN = MarkdownIt("commonmark")


class Source(Contract):
    source_id: Identifier
    title: Summary
    url: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    snippet: Annotated[str, StringConstraints(max_length=2000)]
    provider_rank: int = Field(ge=1, le=1000)
    retrieved_at: datetime

    @model_validator(mode="after")
    def public_url(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or re.search(r"[\s\\<>\x00-\x1f]", self.url)):
            raise ValueError("Source URL must be HTTP(S) without credentials")
        host = parsed.hostname.rstrip(".")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if ("." not in host or host.endswith((".localhost", ".local", ".internal"))
                    or re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", host.rsplit(".", 1)[-1])):
                raise ValueError("Source URL must identify a public host") from None
        else:
            if not address.is_global or address.is_multicast:
                raise ValueError("Source URL must identify a public host")
        if self.retrieved_at.tzinfo is None:
            raise ValueError("Source timestamp requires a timezone")
        return self


class SourceManifest(Contract):
    provider: Literal["searxng"]
    request_id: Identifier
    query: Annotated[str, StringConstraints(min_length=1, max_length=400)]
    sources: tuple[Source, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def distinct_sources(self):
        if len({item.url for item in self.sources}) != len(self.sources) or len(
            {item.source_id for item in self.sources},
        ) != len(self.sources):
            raise ValueError("Source URLs and IDs must be distinct")
        return self


def markdown_evidence(text: str) -> tuple[set[str], set[str]]:
    headings, links = set(), set()
    heading = False
    # Tokenize only. Raw HTML and images are rejected rather than rendered.
    for token in _MARKDOWN.parse(text):
        if token.type == "html_block":
            raise ValueError("Markdown deliverables may not contain raw HTML")
        if token.type in {"heading_open", "heading_close"}:
            heading = token.type == "heading_open"
        if token.type != "inline":
            continue
        children = token.children or []
        if heading:
            headings.add("".join(child.content for child in children if child.type in {"text", "code_inline"}))
        for child in children:
            if child.type in {"html_inline", "image"}:
                raise ValueError("Markdown deliverables may not contain raw HTML or embedded images")
            if child.type == "link_open":
                links.add(str(child.attrGet("href")))
    return headings, links
