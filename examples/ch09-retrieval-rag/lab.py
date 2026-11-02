#!/usr/bin/env python3
"""Offline retrieval control lab. No network, model, or real authentication."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import json
import math
import re


@dataclass(frozen=True)
class Scope:
    """Trusted fixture supplied by the application, never parsed from query text."""
    tenant_id: str
    groups: frozenset[str]


@dataclass(frozen=True)
class Query:
    text: str
    error_code: str | None
    os: str | None
    client_version: str | None

    @classmethod
    def parse(cls, text: str) -> Query:
        codes = [code.upper() for code in re.findall(r"\bE\d{3}\b", text, re.I)]
        versions = re.findall(r"\b\d+\.\d+\b", text)
        platforms = [name for name in ("Windows", "macOS")
                     if name.lower() in text.lower()]
        # Multiple conflicting values are deliberately unresolved.
        return cls(text, codes[0].upper() if len(set(codes)) == 1 else None,
                   platforms[0] if len(platforms) == 1 else None,
                   versions[0] if len(set(versions)) == 1 else None)


def checked_rewrite(original: str, rewritten: str) -> str:
    """A rewrite must preserve the exact error-code set, including no additions."""
    extract = lambda text: set(re.findall(r"\bE\d{3}\b", text.upper()))
    if extract(original) != extract(rewritten):
        raise ValueError("rewrite_changed_error_code")
    return rewritten


def tokenize(text: str) -> list[str]:
    """ASCII terms + a tiny explicit Chinese lexicon, not production segmentation."""
    terms = re.findall(r"[a-z]+\d*|\d+(?:\.\d+)?", text.lower())
    terms += [word for word in ("连接", "失败", "时间", "检查", "重装", "客户端")
              if word in text]
    return terms


def bm25(query: str, documents: dict[str, str], *, k1: float = 1.2,
         b: float = 0.75) -> list[tuple[str, float]]:
    """Small exact BM25 ranking; ties use stable IDs for reproducibility."""
    if not documents:
        return []
    bags = {key: Counter(tokenize(text)) for key, text in documents.items()}
    lengths = {key: sum(bag.values()) for key, bag in bags.items()}
    avg_length = sum(lengths.values()) / len(bags) or 1.0
    result = []
    for key, bag in bags.items():
        score = 0.0
        for term in set(tokenize(query)):
            frequency = bag[term]
            if not frequency:
                continue
            df = sum(term in other for other in bags.values())
            idf = math.log(1 + (len(bags) - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (1 - b + b * lengths[key] / avg_length)
            score += idf * frequency * (k1 + 1) / denominator
        if score > 0:
            result.append((key, score))
    return sorted(result, key=lambda item: (-item[1], item[0]))


def rrf(rankings: list[list[str]], *, constant: int = 60) -> list[tuple[str, float]]:
    """Rank-based fusion; duplicate IDs within one route contribute only once."""
    if constant < 0:
        raise ValueError("invalid_rrf_constant")
    scores: Counter[str] = Counter()
    for ranking in rankings:
        unique = list(dict.fromkeys(ranking))
        for rank, key in enumerate(unique, start=1):
            scores[key] += 1 / (constant + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


@dataclass(frozen=True)
class Document:
    tenant_id: str
    groups: frozenset[str]
    active_generation: str
    deleted: bool = False


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    generation: str
    text: str
    os: str
    client_version: str
    error_code: str
    locator: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    role: str = "answer"


@dataclass(frozen=True)
class IndexRow:
    """Untrusted/stale projection metadata; contains an ID, not authoritative text."""
    chunk_id: str
    generation: str
    tenant_id: str
    groups: frozenset[str]


class SearchUnavailable(Exception):
    pass


class RetrievalLab:
    def __init__(self) -> None:
        self.documents = {
            "vpn": Document("demo-company", frozenset({"employee"}), "g2"),
            "vpn-mac": Document("demo-company", frozenset({"employee"}), "g1"),
            "retired": Document("demo-company", frozenset({"employee"}), "g1", True),
            "hr": Document("demo-company", frozenset({"hr"}), "g1"),
            "other": Document("other-company", frozenset({"employee"}), "g1"),
        }
        self.chunks = {
            "vpn-current": Chunk("vpn-current", "vpn", "g2",
                "Windows 5.2 VPN E104 连接失败：先检查系统时间；仍失败时联系 IT。",
                "Windows", "5.2", "E104", "kb://it/vpn?revision=2#e104",
                required=("vpn-condition",), optional=("hr-private",)),
            "vpn-condition": Chunk("vpn-condition", "vpn", "g2",
                "适用条件：Windows 客户端 5.2；不要套用到 macOS。",
                "Windows", "5.2", "E104", "kb://it/vpn?revision=2#scope",
                role="condition"),
            "vpn-old": Chunk("vpn-old", "vpn", "g1",
                "Windows 5.2 VPN E104 连接失败：重装客户端。",
                "Windows", "5.2", "E104", "kb://it/vpn?revision=1#e104"),
            "vpn-mac": Chunk("vpn-mac", "vpn-mac", "g1",
                "macOS 5.2 VPN E104 连接失败：检查客户端证书。",
                "macOS", "5.2", "E104", "kb://it/vpn-mac?revision=1#e104"),
            "deleted-old": Chunk("deleted-old", "retired", "g1",
                "Windows 5.2 VPN E104 撤回的处理建议。",
                "Windows", "5.2", "E104", "kb://it/retired#e104"),
            "hr-private": Chunk("hr-private", "hr", "g1",
                "HR_PRIVATE_FIXTURE 不应出现在普通员工回答中。",
                "Windows", "5.2", "E104", "kb://hr/private#e104"),
            "other-private": Chunk("other-private", "other", "g1",
                "OTHER_TENANT_FIXTURE 不应出现在当前租户回答中。",
                "Windows", "5.2", "E104", "kb://other/private#e104"),
        }
        self.index = [IndexRow(key, chunk.generation,
                      self.documents[chunk.document_id].tenant_id,
                      self.documents[chunk.document_id].groups)
                      for key, chunk in self.chunks.items()]
        # A hand-authored second ranking, deliberately NOT a vector model.
        self.fixture_ranking = ["vpn-current", "vpn-old", "vpn-mac", "deleted-old"]
        self.backend_available = True

    def eligible(self, scope: Scope, row: IndexRow) -> Chunk | None:
        chunk = self.chunks.get(row.chunk_id)
        if chunk is None:
            return None
        document = self.documents.get(chunk.document_id)
        if (document is None or document.tenant_id != scope.tenant_id
                or not document.groups.intersection(scope.groups)
                or document.deleted
                or document.active_generation != chunk.generation
                or row.generation != chunk.generation):
            return None
        return chunk

    def search(self, scope: Scope, query: Query, *, candidate_k: int,
               dirty_rows: tuple[IndexRow, ...] = ()) -> list[list[IndexRow]]:
        if not self.backend_available:
            raise SearchUnavailable("search_unavailable")
        # Prefilter reduces the search space; it is not a replacement for eligible().
        rows = {row.chunk_id: row for row in self.index
                if row.tenant_id == scope.tenant_id and row.groups.intersection(scope.groups)}
        texts = {key: self.chunks[key].text for key in rows if key in self.chunks}
        lexical = [rows[key] for key, _ in bm25(query.text, texts)[:candidate_k]]
        fixture = [rows[key] for key in self.fixture_ranking
                   if key in rows][:candidate_k] if query.error_code == "E104" else []
        # Test-only injection models stale/incorrect filter metadata, not an API input.
        return [lexical + list(dirty_rows), fixture]

    def retrieve(self, scope: Scope, text: str, *, top_k: int = 2,
                 candidate_k: int = 10, max_chars: int = 180,
                 rewrite: str | None = None,
                 dirty_rows: tuple[IndexRow, ...] = ()) -> dict:
        if top_k < 1 or candidate_k < 1 or max_chars < 1:
            raise ValueError("invalid_budget")
        query = Query.parse(text)
        missing = [name for name in ("error_code", "os", "client_version")
                   if getattr(query, name) is None]
        if missing:
            return {"status": "clarify", "missing": missing, "evidence": []}
        retrieval_text = checked_rewrite(text, rewrite) if rewrite is not None else text
        retrieval_query = replace(query, text=retrieval_text)
        try:
            routes = self.search(scope, retrieval_query, candidate_k=candidate_k,
                                 dirty_rows=dirty_rows)
        except SearchUnavailable:
            return {"status": "error", "code": "search_unavailable", "evidence": []}
        authorized_routes, authorized = [], {}
        for route in routes:
            authorized_ids = []
            for row in route:
                chunk = self.eligible(scope, row)
                if chunk is None:
                    continue
                authorized[chunk.chunk_id] = chunk
                authorized_ids.append(chunk.chunk_id)
            authorized_routes.append(authorized_ids)
        fused = rrf(authorized_routes)
        # Rule reranking only: first enforce applicability, then keep fusion order.
        applicable = [authorized[key] for key, _ in fused
                      if authorized[key].role == "answer"
                      and (authorized[key].error_code, authorized[key].os,
                          authorized[key].client_version)
                      == (query.error_code, query.os, query.client_version)]
        if not applicable:
            return {"status": "abstain", "reason": "no_applicable_evidence",
                    "evidence": []}

        def complete_group(root_id: str) -> list[Chunk] | None:
            """Every required dependency must resolve; a denied optional root is skipped."""
            group, pending, visited = [], [root_id], set()
            while pending:
                key = pending.pop(0)
                if key in visited:
                    continue
                visited.add(key)
                chunk = self.chunks.get(key)
                row = IndexRow(key, chunk.generation, "", frozenset()) if chunk else None
                checked = self.eligible(scope, row) if row else None
                if checked is None or (
                        checked.os, checked.client_version, checked.error_code) != (
                        query.os, query.client_version, query.error_code):
                    return None
                group.append(checked)
                pending.extend(checked.required)
            return group

        selected, seen, used, accepted_seeds = [], set(), 0, []
        complete_group_exists = False
        for seed in applicable:
            if seed.chunk_id in seen:
                continue
            # Missing, denied, stale, deleted or inapplicable required text rejects the seed.
            resolved = complete_group(seed.chunk_id)
            if resolved is None:
                continue
            complete_group_exists = True
            group = [chunk for chunk in resolved if chunk.chunk_id not in seen]
            # The seed and all required dependencies form an indivisible budget unit.
            group_chars = sum(len(chunk.text) for chunk in group)
            if used + group_chars > max_chars:
                continue
            selected.extend(group)
            seen.update(chunk.chunk_id for chunk in group)
            used += group_chars
            accepted_seeds.append(seed)
            if len(accepted_seeds) >= top_k:
                break
        # Finish selecting required groups before optional context can use any budget.
        for seed in accepted_seeds:
            for optional_id in seed.optional:
                resolved_optional = complete_group(optional_id)
                if resolved_optional is None:
                    continue
                extra = [chunk for chunk in resolved_optional if chunk.chunk_id not in seen]
                extra_chars = sum(len(chunk.text) for chunk in extra)
                if used + extra_chars <= max_chars:
                    selected.extend(extra)
                    seen.update(chunk.chunk_id for chunk in extra)
                    used += extra_chars
        if not selected:
            reason = "evidence_exceeds_budget" if complete_group_exists else "no_complete_evidence"
            return {"status": "abstain", "reason": reason, "evidence": []}
        evidence = [{"citation_id": f"S{number}", "chunk_id": chunk.chunk_id,
                     "generation": chunk.generation, "text": chunk.text,
                     "locator": chunk.locator}
                    for number, chunk in enumerate(selected, start=1)]
        return {"status": "evidence_ready", "evidence": evidence,
                "used_chars": used}


def validate_citations(claims: list[dict], evidence: list[dict]) -> bool:
    """Checks referenced IDs exist; does NOT test whether evidence entails a claim."""
    allowed = {item["citation_id"] for item in evidence}
    return bool(claims) and all(
        isinstance(claim.get("text"), str) and bool(claim["text"].strip())
        and isinstance(claim.get("citations"), list) and bool(claim["citations"])
        and all(isinstance(key, str) and key in allowed for key in claim["citations"])
        for claim in claims)


def recall_at_k(ranking: list[str], relevant: set[str], k: int) -> float | None:
    if not relevant:
        return None  # No-answer questions need a separate abstention metric.
    return len(set(ranking[:k]) & relevant) / len(relevant)


def reciprocal_rank(ranking: list[str], relevant: set[str]) -> float:
    return next((1 / rank for rank, key in enumerate(ranking, start=1)
                 if key in relevant), 0.0)


def demo() -> dict:
    lab = RetrievalLab()
    scope = Scope("demo-company", frozenset({"employee"}))
    dirty = tuple(IndexRow(key, lab.chunks[key].generation, "demo-company",
                           frozenset({"employee"}))
                  for key in ("hr-private", "other-private"))
    result = lab.retrieve(scope, "Windows 5.2 VPN E104 连接失败", dirty_rows=dirty)
    claims = [{"text": "先检查系统时间；仍失败时联系 IT。", "citations": ["S1"]}]
    lab.backend_available = False
    failed = lab.retrieve(scope, "Windows 5.2 VPN E104 连接失败")
    lab.backend_available = True
    no_evidence = lab.retrieve(scope, "Windows 9.0 VPN E999 连接失败")
    ranking = [key for key, _ in rrf([["a", "b"], ["b", "c"]])]
    return {
        "retrieval": result,
        "clarify": lab.retrieve(scope, "VPN E104 连接失败")["status"],
        "citation_ids_valid": validate_citations(claims, result["evidence"]),
        "backend_failure": failed["status"],
        "no_applicable_evidence": no_evidence["status"],
        "rrf_demo": {"ranking": ranking, "recall_at_2": recall_at_k(ranking, {"b", "c"}, 2),
                     "reciprocal_rank": reciprocal_rank(ranking, {"b", "c"})},
        "second_route": "fixed_manual_ranking_not_embedding",
    }


if __name__ == "__main__":
    print(json.dumps(demo(), ensure_ascii=False, indent=2))
