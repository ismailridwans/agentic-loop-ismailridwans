"""Splits a document into numbered chunks. No LLM here.

raw[start:end] == text has to hold exactly. Claims store these offsets and we
slice the source with them later to check a quote is real.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_BLOCK = re.compile(r"[^\n]+(?:\n[^\n]+)*")   # run of non-empty lines
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_SECTION = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s+(.*)$")   # "2.1 System Logs"


def segment(raw: str) -> list[dict]:
    chunks: list[dict] = []
    section = ""
    heading = ""

    for n, block in enumerate(_BLOCK.finditer(raw)):
        text = block.group(0)
        head = _HEADING.match(text)

        if head:
            title = head.group(1).strip()
            numbered = _SECTION.match(title)
            if numbered:
                section, heading = numbered.group(1), numbered.group(2).strip()
                chunk_section = section
            else:
                # the doc title has no number, so don't let anything under it
                # inherit whatever section we were in
                heading = title
                chunk_section = ""
            kind = "heading"
        else:
            chunk_section = section
            kind = "text"

        chunks.append({
            "id": f"c{n:03d}",
            "section": chunk_section,
            "heading": heading,
            "kind": kind,
            "text": text,
            "start": block.start(),
            "end": block.end(),
        })

    return chunks


def load(path: str | Path) -> tuple[str, list[dict]]:
    raw = Path(path).read_text(encoding="utf-8")
    return raw, segment(raw)


def doc_id(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
