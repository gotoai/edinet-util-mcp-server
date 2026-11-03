"""Ranked peers within the TSE 33-industry: financial scale plus business text (design D9).

Reviewed 2026-11-03 (Temp/20261103_Company_Clustering). For two companies in the
same industry:

- financial: exp(-d), d = root mean square of the differences in ln(revenue),
  ln(total assets) and ln(employees), over the features both have. A peer half or
  twice the size on every feature scores exp(-ln 2) = 0.5.
- text: TF-IDF over character 2-3-grams of the annual report's 事業の内容 and
  経営方針・経営環境及び対処すべき課題等 (first 1,500 characters of each, company names
  and digits masked), cosine similarity, scaled per company so that its best match
  within the industry is 1.0. The vocabulary is fitted on every company.
- score = (1 - w) * financial + w * text, w = 0.5.

Only companies in the same industry are candidates: across industries, text
similarity mostly reflects shared reporting vocabulary, not shared markets (イオン
and りそな use the same favoured business terms).

Deterministic: TF-IDF has no randomness, and ties are broken by EDINET code.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

ALGORITHM = "same-industry-scale-text-v2"
TEXT_WEIGHT = 0.5
SECTION_CHARS = 1500
TEXT_SECTIONS = ("DescriptionOfBusinessTextBlock",
                 "BusinessPolicyBusinessEnvironmentIssuesToAddressEtcTextBlock")
FEATURES = (("revenue", "売上高"), ("total_assets", "総資産"), ("employees", "従業員"))
PARAMS = (f"score=(1-w)*financial+w*text, w={TEXT_WEIGHT}; financial=exp(-rms of log "
          "differences in revenue, total_assets, employees); text=TF-IDF char 2-3-gram cosine "
          f"of 事業の内容 + 経営方針等 (first {SECTION_CHARS} chars each), scaled to the best "
          "match in the industry; candidates=same TSE 33-industry")


@dataclass(frozen=True)
class Company:
    code: str
    industry: str | None
    revenue: float | None
    total_assets: float | None
    employees: float | None
    text: str = ""

    def logs(self) -> dict[str, float]:
        return {name: math.log(value) for name, _ in FEATURES
                if (value := getattr(self, name)) is not None and value > 0}


@dataclass(frozen=True)
class Peer:
    code: str
    peer: str
    rank: int
    score: float
    reason: str


def report_text(sections: dict[str, str]) -> str:
    """The text compared between companies, from the report's text blocks by element."""
    return " ".join(_clean(sections.get(element, "")) for element in TEXT_SECTIONS)


def rank(companies: list[Company], *, top_n: int, text_weight: float = TEXT_WEIGHT
         ) -> list[Peer]:
    members = sorted((c for c in companies if c.industry and c.logs()), key=lambda c: c.code)
    if len(members) < 2:
        return []
    text = _text_similarity(members)
    groups: dict[str, list[int]] = defaultdict(list)
    for index, company in enumerate(members):
        groups[company.industry].append(index)

    result: list[Peer] = []
    for indexes in groups.values():
        logs = [members[i].logs() for i in indexes]
        within = text[np.ix_(indexes, indexes)].copy()
        np.fill_diagonal(within, 0)
        best = within.max(axis=1)
        for a, i in enumerate(indexes):
            scored = []
            for b, j in enumerate(indexes):
                if i == j:
                    continue
                distance = _distance(logs[a], logs[b])
                financial = math.exp(-distance) if distance is not None else 0.0
                textual = float(within[a, b] / best[a]) if best[a] > 0 else 0.0
                score = (1 - text_weight) * financial + text_weight * textual
                scored.append((-score, members[j].code, j, financial, textual))
            scored.sort()
            for position, (negative, _, j, financial, textual) in enumerate(scored[:top_n], 1):
                result.append(Peer(members[i].code, members[j].code, position,
                                   round(-negative, 4),
                                   _reason(members[i], members[j], financial, textual)))
    return sorted(result, key=lambda p: (p.code, p.rank))


def _text_similarity(members: list[Company]) -> np.ndarray:
    documents = [c.text for c in members]
    if not any(documents):
        return np.zeros((len(members), len(members)), dtype=np.float32)
    vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 3), sublinear_tf=True,
                                 min_df=2 if len(members) > 2 else 1, max_df=0.3)
    try:
        matrix = vectorizer.fit_transform(documents)
    except ValueError:  # no term survives min_df / max_df, e.g. a tiny test set
        return np.zeros((len(members), len(members)), dtype=np.float32)
    return linear_kernel(matrix, matrix).astype(np.float32)


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"^\s*\S{0,3}\s*【[^】]{1,30}】", "", text)
    text = re.sub(r"[^\s、。()「」]*株式会社[^\s、。()「」]*", " ", text)
    return re.sub(r"\d+", "0", text)[:SECTION_CHARS]


def _distance(a: dict[str, float], b: dict[str, float]) -> float | None:
    shared = [name for name in a if name in b]
    if not shared:
        return None
    return math.sqrt(sum((a[name] - b[name]) ** 2 for name in shared) / len(shared))


def _reason(company: Company, other: Company, financial: float, textual: float) -> str:
    parts = [f"同業種（{company.industry}）"]
    for name, label in FEATURES:
        mine, theirs = getattr(company, name), getattr(other, name)
        if mine and theirs and mine > 0 and theirs > 0:
            parts.append(f"{label}{theirs / mine:.2f}倍")
    parts.append(f"規模の類似度{financial:.2f}")
    parts.append(f"事業内容の類似度{textual:.2f}")
    return "・".join(parts)
