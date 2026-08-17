"""Milestone 2 — memory.

Why this matters more here than in most agent projects: a contradiction is by
definition two statements in *different places*. An agent with no memory reads a
chunk, extracts some claims, and has nothing to compare them against — every
earlier round's work is gone. So memory is not a bolt-on; without it this task is
impossible, which is also what makes it easy to demonstrate honestly (turn it off
and the findings disappear).

Backend is ChromaDB. The operation the agent needs most often is "find claims
similar to this one", which is exactly a vector search — Chroma is the search
engine, not decoration. It also runs locally, so the demo can't break on someone
else's rate limit.

``DictMemory`` is the same interface without the vector store. Tests use it so the
suite stays fast and offline, and ``--no-memory`` uses ``NullMemory`` to prove the
agent is worse without any of this.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

# Three namespaces, three jobs.
CLAIMS = "claims"        # every statement found — the pool candidates come from
FINDINGS = "findings"    # confirmed contradictions
REJECTED = "rejected"    # pairs already cleared, so they are never re-checked
NAMESPACES = (CLAIMS, FINDINGS, REJECTED)


class MemoryError_(RuntimeError):
    """A memory operation failed. The loop degrades rather than stopping."""


class Memory(Protocol):
    def save(self, records: list[dict], namespace: str) -> dict: ...
    def recall(self, query: str, namespace: str, k: int = 6) -> list[dict]: ...
    def clear(self, namespace: str | None = None) -> dict: ...


# --------------------------------------------------------------------------- #
# The real backend
# --------------------------------------------------------------------------- #


class ChromaMemory:
    """Persistent vector memory. One Chroma collection per namespace."""

    def __init__(self, persist_dir: str = ".memory") -> None:
        try:
            import chromadb
        except ImportError as exc:  # pragma: no cover
            raise MemoryError_(
                "chromadb is not installed. `pip install -r requirements.txt`, "
                "or run with --no-memory."
            ) from exc

        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collections = {
            ns: self._client.get_or_create_collection(ns) for ns in NAMESPACES
        }

    def _col(self, namespace: str):
        if namespace not in self._collections:
            raise MemoryError_(f"unknown namespace: {namespace}")
        return self._collections[namespace]

    def save(self, records: list[dict], namespace: str) -> dict:
        """Each record needs an 'id' and a 'text'; anything else becomes metadata."""
        if not records:
            return {"saved": 0}

        ids, docs, metas = [], [], []
        for r in records:
            ids.append(str(r["id"]))
            docs.append(r["text"])
            # Chroma metadata must be flat scalars, so anything nested is JSON.
            metas.append(
                {k: (v if isinstance(v, (str, int, float, bool)) else json.dumps(v))
                 for k, v in r.items() if k not in ("id", "text") and v is not None}
            )

        self._col(namespace).upsert(ids=ids, documents=docs, metadatas=metas)
        return {"saved": len(ids), "namespace": namespace}

    def recall(self, query: str, namespace: str, k: int = 6) -> list[dict]:
        col = self._col(namespace)
        if col.count() == 0:
            return []

        res = col.query(query_texts=[query], n_results=min(k, col.count()))
        out = []
        for i, doc in enumerate(res["documents"][0]):
            meta = res["metadatas"][0][i] or {}
            out.append({"id": res["ids"][0][i], "text": doc,
                        "distance": res["distances"][0][i], **meta})
        return out

    def clear(self, namespace: str | None = None) -> dict:
        targets = [namespace] if namespace else list(NAMESPACES)
        for ns in targets:
            self._client.delete_collection(ns)
            self._collections[ns] = self._client.get_or_create_collection(ns)
        return {"cleared": targets}


# --------------------------------------------------------------------------- #
# Same interface, no vector store. Used by tests and as a fallback.
# --------------------------------------------------------------------------- #

_STOPWORDS = {"the", "a", "an", "of", "for", "to", "in", "and", "or", "is", "are"}


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _STOPWORDS}


class DictMemory:
    """In-process store ranked by word overlap instead of embeddings."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict]] = {ns: {} for ns in NAMESPACES}

    def save(self, records: list[dict], namespace: str) -> dict:
        if namespace not in self._data:
            raise MemoryError_(f"unknown namespace: {namespace}")
        for r in records:
            self._data[namespace][str(r["id"])] = dict(r)
        return {"saved": len(records), "namespace": namespace}

    def recall(self, query: str, namespace: str, k: int = 6) -> list[dict]:
        if namespace not in self._data:
            raise MemoryError_(f"unknown namespace: {namespace}")
        q = _tokens(query)
        scored = []
        for r in self._data[namespace].values():
            t = _tokens(r.get("text", "") + " " + str(r.get("subject", "")))
            overlap = len(q & t) / len(q | t) if (q | t) else 0.0
            scored.append((overlap, r))
        scored.sort(key=lambda s: -s[0])
        return [dict(r, distance=round(1 - s, 3)) for s, r in scored[:k]]

    def clear(self, namespace: str | None = None) -> dict:
        targets = [namespace] if namespace else list(NAMESPACES)
        for ns in targets:
            self._data[ns] = {}
        return {"cleared": targets}


class NullMemory:
    """Remembers nothing. What --no-memory uses, to show the difference."""

    def save(self, records: list[dict], namespace: str) -> dict:
        return {"saved": 0}

    def recall(self, query: str, namespace: str, k: int = 6) -> list[dict]:
        return []

    def clear(self, namespace: str | None = None) -> dict:
        return {"cleared": []}


# --------------------------------------------------------------------------- #
# Module-level API — the three functions the challenge asks for
# --------------------------------------------------------------------------- #

_BACKEND: Memory = NullMemory()


def configure(backend: str = "chroma", persist_dir: str = ".memory") -> Memory:
    global _BACKEND
    if backend == "chroma":
        _BACKEND = ChromaMemory(persist_dir)
    elif backend == "dict":
        _BACKEND = DictMemory()
    else:
        _BACKEND = NullMemory()
    return _BACKEND


def use(backend: Memory) -> Memory:
    global _BACKEND
    _BACKEND = backend
    return _BACKEND


def save(records: list[dict], namespace: str) -> dict:
    return _BACKEND.save(records, namespace)


def recall(query: str, namespace: str, k: int = 6) -> list[dict]:
    return _BACKEND.recall(query, namespace, k)


def clear(namespace: str | None = None) -> dict:
    return _BACKEND.clear(namespace)


# --------------------------------------------------------------------------- #
# Helpers the loop uses
# --------------------------------------------------------------------------- #


def pair_id(a: str, b: str) -> str:
    """Order-independent key, so A/B and B/A are the same rejected pair."""
    lo, hi = sorted((a, b))
    return f"{lo}|{hi}"


def remember_round(backend: Memory, *, claims: list[dict], finding: dict | None,
                   rejected: dict | None) -> dict:
    """Write everything one round learned. Called after reflect."""
    written = {"claims": 0, "findings": 0, "rejected": 0}

    if claims:
        written["claims"] = backend.save(
            [{"id": c["id"], "text": c["text"], "section": c["section"],
              "type": c["type"], "subject": c["subject"]} for c in claims],
            CLAIMS,
        )["saved"]

    if finding:
        written["findings"] = backend.save(
            [{"id": finding["id"],
              "text": f"{finding['type']} between §{finding['sections'][0]} "
                      f"and §{finding['sections'][1]}: {finding['explanation']}",
              "type": finding["type"]}],
            FINDINGS,
        )["saved"]

    if rejected:
        written["rejected"] = backend.save(
            [{"id": pair_id(*rejected["claims"]),
              "text": rejected["why"], "guard": rejected["guard"]}],
            REJECTED,
        )["saved"]

    return written
