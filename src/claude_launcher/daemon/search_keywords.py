"""Incremental BM25 retrieval over the unified corpus's persisted passages."""
from __future__ import annotations

import hashlib
import math
import re
import threading
import unicodedata
from collections import Counter, defaultdict


def terms(text):
    # Preserve filenames and issue IDs, while ignoring surrounding punctuation.
    return re.findall(r"[a-z0-9_]+(?:[.-][a-z0-9_]+)*|[^\W\d_]+",
                      unicodedata.normalize("NFKC", text).casefold())


def group_key(doc_id, meta):
    if meta.get("kind") in ("beads", "comment") and meta.get("issue"):
        return ("issue", meta.get("root"), meta["issue"])
    # Unified records have one document per passage, with a numeric suffix.
    source, sep, suffix = doc_id.rpartition(":")
    return ("source", source if sep and suffix.isdigit() else doc_id)


class KeywordIndex:
    """Derived from display metadata; requires no new embedding or disk format."""

    def __init__(self):
        self.postings = defaultdict(dict)
        self.documents = {}
        self.total_length = 0
        self.lock = threading.RLock()

    def put(self, doc_id, meta):
        with self.lock:
            self._put(doc_id, meta)

    def _put(self, doc_id, meta):
        text = "\n".join(str(v or "") for v in
                         (doc_id, meta.get("title"), meta.get("excerpt")))
        previous = self.documents.get(doc_id)
        digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()
        if previous and previous[0] == digest:
            return
        self.drop(doc_id)
        counts = Counter(terms(text))
        length = sum(counts.values())
        self.documents[doc_id] = (digest, tuple(counts), length)
        self.total_length += length
        for term, count in counts.items():
            self.postings[term][doc_id] = count

    def drop(self, doc_id):
        with self.lock:
            self._drop(doc_id)

    def _drop(self, doc_id):
        previous = self.documents.pop(doc_id, None)
        if previous is None:
            return
        _, counts, length = previous
        self.total_length -= length
        for term in counts:
            posting = self.postings[term]
            posting.pop(doc_id, None)
            if not posting:
                del self.postings[term]

    def rank(self, query, k, *, exclude=()):
        with self.lock:
            return self._rank(query, k, exclude=exclude)

    def _rank(self, query, k, *, exclude=()):
        count = len(self.documents)
        if not count or k <= 0:
            return []
        average = max(1, self.total_length / count)
        scores = defaultdict(float)
        skip = set(exclude)
        for term in sorted(set(terms(query))):
            posting = self.postings.get(term, {})
            idf = math.log(1 + (count - len(posting) + 0.5) / (len(posting) + 0.5))
            for doc_id, frequency in posting.items():
                if doc_id in skip:
                    continue
                length = self.documents[doc_id][2]
                scores[doc_id] += idf * frequency * 2.2 / (
                    frequency + 1.2 * (0.25 + 0.75 * length / average))
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:k]


def fuse(index, vector_hits, keyword_hits, exact_hits, budget):
    """Reciprocal rank fusion per source, so repeated passages add no votes."""
    scores, members, representatives = defaultdict(float), defaultdict(list), {}
    exact_groups = set()
    for hits in (keyword_hits, vector_hits):
        seen = set()
        for doc_id, _ in hits:
            entry = index.entries.get(doc_id)
            if entry is None:
                continue  # A background sync may have removed a retrieved passage.
            group = group_key(doc_id, entry.meta)
            if len(members[group]) < 3 and doc_id not in members[group]:
                members[group].append(doc_id)
            representatives.setdefault(group, doc_id)
            if group not in seen:
                seen.add(group)
                scores[group] += 1 / (60 + len(seen))
    for doc_id in exact_hits:
        entry = index.entries.get(doc_id)
        if entry is None:
            continue
        group = group_key(doc_id, entry.meta)
        exact_groups.add(group)
        if len(members[group]) < 3 and doc_id not in members[group]:
            members[group].append(doc_id)
        representatives.setdefault(group, doc_id)
        scores.setdefault(group, 0)
    ordered = sorted(scores, key=lambda group: (group not in exact_groups, -scores[group]))[:budget]
    return ([representatives[g] for g in ordered],
            {representatives[g]: scores[g] for g in ordered},
            {representatives[g]: members[g][:3] for g in ordered})
