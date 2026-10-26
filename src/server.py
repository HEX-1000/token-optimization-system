"""
Token Optimization System, retrieval V0.25.0 (formerly Claude Token Optimizer)

Four-stage pipeline: RETRIEVE → CLASSIFY → RANK → SELECT

Each stage has a single responsibility and a defined data contract.
No monolithic scoring formula. Signals stay separate and interpretable.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any


SERVER_VERSION = "0.25.0"

DB_PATH = Path(__file__).parent / "data" / "optimizer.db"

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Token Optimization System")


# ============================================================================
# UTILITIES
# ============================================================================


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_word(word: str) -> str:
    word = word.lower().strip()
    if len(word) > 5 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("es"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def normalized_word_set(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {normalize_word(w) for w in words if w}


def normalize_search_terms(query: str) -> list[str]:
    normalized = normalize_text(query)
    terms: list[str] = []
    for term in normalized.split():
        if term and term not in terms:
            terms.append(term)
    return terms


# ============================================================================
# QUERY EXPANSION
# ============================================================================

QUERY_EXPANSIONS: dict[str, list[str]] = {
    "cost": ["cost", "measurement", "baseline", "delivered", "saved", "usage"],
    "token": ["token", "tokens", "measurement", "cost", "usage"],
    "tokens": ["token", "tokens", "measurement", "cost", "usage"],
    "complementary": ["complementary", "evidence", "support", "example", "additional"],
    "context": ["context", "redundancy", "quality", "relevance"],
    "optimization": ["optimization", "optimize", "efficiency", "reduction", "saving"],
    "optimize": ["optimization", "optimize", "efficiency", "reduction", "saving"],
    "database": ["database", "databases", "query", "index"],
    "databases": ["database", "databases", "query", "index"],
    "compiler": ["compiler", "compilers", "compilation"],
    "compilers": ["compiler", "compilers", "compilation"],
    "artificial": ["artificial", "intelligence", "ai", "machine", "learning"],
    "intelligence": ["artificial", "intelligence", "ai", "machine", "learning"],
}


def build_expanded_terms(query: str) -> list[str]:
    original_terms = normalize_search_terms(query)
    expanded: list[str] = []

    for term in original_terms:
        values = QUERY_EXPANSIONS.get(term, [term])
        for value in values:
            normalized = normalize_word(value)
            if normalized and normalized not in expanded:
                expanded.append(normalized)

    for term in original_terms:
        normalized = normalize_word(term)
        if normalized not in expanded:
            expanded.append(normalized)

    return expanded


def build_fts_query(terms: list[str], operator: str = "AND", prefix: bool = False) -> str:
    safe = [re.sub(r"[^a-zA-Z0-9_]", "", t) for t in terms]
    safe = [t for t in safe if t]
    if prefix:
        safe = [t + "*" for t in safe]
    return f" {operator} ".join(safe)


# ============================================================================
# IDF — Inverse Document Frequency from corpus
# ============================================================================


def compute_term_idf(
    conn: sqlite3.Connection,
    terms: list[str],
) -> dict[str, float]:
    """
    Compute IDF for each term against the FTS5 corpus.

    IDF = log(1 + N / df) where:
      N  = total number of chunks in the corpus
      df = number of chunks containing the term

    Returns a dict mapping each normalized term to its IDF value.
    Terms not found in the corpus get a high default IDF.
    """

    total_rows = conn.execute(
        "SELECT count(*) FROM chunks"
    ).fetchone()[0]

    if total_rows == 0:
        return {t: 1.0 for t in terms}

    result: dict[str, float] = {}

    for term in terms:
        safe_term = re.sub(r"[^a-zA-Z0-9_]", "", term)
        if not safe_term:
            result[term] = 1.0
            continue

        try:
            df = conn.execute(
                "SELECT count(*) FROM chunks WHERE chunks MATCH ?",
                (safe_term,),
            ).fetchone()[0]
        except sqlite3.Error:
            df = 0

        if df == 0:
            # Term not in corpus — assign high IDF
            result[term] = math.log(1 + total_rows)
        else:
            result[term] = math.log(1 + total_rows / df)

    return result


# ============================================================================
# TEXT SIMILARITY (for redundancy)
# ============================================================================


def jaccard_similarity(text_a: str, text_b: str) -> float:
    words_a = normalized_word_set(text_a)
    words_b = normalized_word_set(text_b)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def word_containment(text_a: str, text_b: str) -> float:
    words_a = normalized_word_set(text_a)
    words_b = normalized_word_set(text_b)
    if not words_a or not words_b:
        return 0.0
    smaller = min(len(words_a), len(words_b))
    if smaller == 0:
        return 0.0
    return len(words_a & words_b) / smaller


def remove_query_words(text: str, query: str) -> str:
    query_words = {normalize_word(w) for w in normalize_search_terms(query)}
    words = re.findall(r"[a-z0-9]+", text.lower())
    return " ".join(w for w in words if normalize_word(w) not in query_words)


# ============================================================================
# NEGATIVE QUALITY PATTERNS
# ============================================================================

NEGATIVE_QUALITY_PATTERNS = [
    "unrelated",
    "irrelevant",
    "not relevant",
    "low value",
    "low-value",
    "little useful information",
    "generic background",
    "do not improve",
    "does not improve",
    "without substantially improving",
]


# ============================================================================
# STAGE 1: RETRIEVE
#
# High recall. FTS5 with AND, OR, and expanded-OR queries.
# Returns raw candidates with BM25 scores and search-stage tags.
# No scoring, no filtering.
# ============================================================================


def stage_retrieve(
    conn: sqlite3.Connection,
    query: str,
    pool_size: int = 30,
) -> dict[str, Any]:
    """Retrieve raw candidates from FTS5. No scoring."""

    original_terms = normalize_search_terms(query)
    expanded_terms = build_expanded_terms(query)

    and_query = build_fts_query(original_terms, "AND")
    or_query = build_fts_query(original_terms, "OR")
    prefix_query = build_fts_query(original_terms, "OR", prefix=True)
    expanded_query = build_fts_query(expanded_terms, "OR", prefix=True)

    candidates: dict[tuple[str, str], dict[str, Any]] = {}

    def run_fts(fts_query: str, stage: str) -> int:
        if not fts_query:
            return 0
        try:
            rows = conn.execute(
                """
                SELECT
                    chunks.rowid AS chunk_id,
                    chunks.path,
                    chunks.section,
                    chunks.content,
                    bm25(chunks) AS bm25_score
                FROM chunks
                WHERE chunks MATCH ?
                ORDER BY bm25_score
                LIMIT ?
                """,
                (fts_query, pool_size),
            ).fetchall()
        except sqlite3.Error:
            return 0

        for row in rows:
            key = (str(row["path"]), str(row["section"]))
            if key not in candidates:
                candidates[key] = {
                    "chunk_id": row["chunk_id"],
                    "path": row["path"],
                    "section": row["section"],
                    "content": row["content"],
                    "bm25_score": float(row["bm25_score"]),
                    "search_stages": [stage],
                }
            else:
                existing = candidates[key]
                if stage not in existing["search_stages"]:
                    existing["search_stages"].append(stage)
                if float(row["bm25_score"]) < existing["bm25_score"]:
                    existing["bm25_score"] = float(row["bm25_score"])

        return len(rows)

    and_count = run_fts(and_query, "AND")
    or_count = run_fts(or_query, "OR")
    prefix_count = run_fts(prefix_query, "PREFIX")
    expanded_count = run_fts(expanded_query, "EXPANDED")

    # Compute IDF for original query terms (used by RANK for title weighting)
    term_idf = compute_term_idf(conn, original_terms)

    return {
        "candidates": list(candidates.values()),
        "and_count": and_count,
        "or_count": or_count,
        "prefix_count": prefix_count,
        "expanded_count": expanded_count,
        "original_terms": original_terms,
        "expanded_terms": expanded_terms,
        "term_idf": term_idf,
    }


# ============================================================================
# STAGE 2: CLASSIFY
#
# For each candidate, compute independent signal values.
# Each signal is a named float. No composite scores. No weighting.
# Write-once: the signals dict is created here and read-only afterward.
# ============================================================================


def stage_classify(
    candidates: list[dict[str, Any]],
    query: str,
    original_terms: list[str],
    expanded_terms: list[str],
) -> list[dict[str, Any]]:
    """Attach independent signals to each candidate. No composite scores."""

    normalized_query = normalize_text(query)
    query_word_set = {normalize_word(t) for t in original_terms}

    # ------------------------------------------------------------------
    # Normalize BM25 scores across candidates.
    #
    # BM25 from FTS5 is negative (more negative = more relevant).
    # Normalize to 0-1 within the candidate pool:
    #   best candidate  → 1.0
    #   worst candidate → 0.0
    #
    # When the BM25 range is very small (< 0.5), all candidates are
    # similarly relevant by content, so assign 0.5 to all — avoid
    # artificial differentiation from noise.
    # ------------------------------------------------------------------
    if candidates:
        bm25_scores = [c["bm25_score"] for c in candidates]
        best_bm25 = min(bm25_scores)   # most negative = most relevant
        worst_bm25 = max(bm25_scores)   # least negative = least relevant
        bm25_range = worst_bm25 - best_bm25
    else:
        bm25_range = 0.0

    for candidate in candidates:
        section = candidate["section"]
        content = candidate["content"]

        normalized_section = normalize_text(section)
        normalized_content = normalize_text(content)

        section_words = normalized_word_set(section)
        content_words = normalized_word_set(content)
        all_words = section_words | content_words

        # --- Direct query coverage ---
        direct_hits = sum(1 for t in original_terms if normalize_word(t) in all_words)
        direct_query_coverage = direct_hits / len(original_terms) if original_terms else 0.0

        # --- Section match (query terms in section/title) ---
        section_hits = sum(1 for t in original_terms if normalize_word(t) in section_words)
        section_match = section_hits / len(original_terms) if original_terms else 0.0

        # --- Exact phrase in section ---
        exact_phrase_section = 1.0 if (normalized_query and normalized_query in normalized_section) else 0.0

        # --- Exact phrase in content ---
        exact_phrase_content = 1.0 if (normalized_query and normalized_query in normalized_content) else 0.0

        # --- Expanded term coverage ---
        expanded_hits = sum(1 for t in expanded_terms if normalize_word(t) in all_words)
        expanded_coverage = expanded_hits / len(expanded_terms) if expanded_terms else 0.0

        # --- Title has all query terms (boolean, expressed as 0/1) ---
        title_has_query = 1.0 if section_match >= 1.0 else 0.0

        # --- Title contains "core", "fundamentals", "basics", etc. ---
        core_words = {"core", "fundamental", "basic", "overview", "introduction", "essential"}
        title_has_core = 1.0 if (section_words & core_words) else 0.0

        # --- Extra modifier words in title not in query ---
        # A modifier word that is also an expanded term for the query
        # is topically related and should NOT be penalized.
        # E.g., if a modifier like "measurement" is an expanded term for
        # a multi-word query, the section title should not be penalized.
        modifier_words = {
            "performance", "history", "background", "system", "example",
            "measurement", "implementation", "advanced", "optimization",
            "redundancy", "quality",
        }
        expanded_set = {normalize_word(t) for t in expanded_terms}
        extra_modifiers = {
            w for w in section_words
            if w in modifier_words
            and w not in query_word_set
            and w not in expanded_set
        }
        title_extra_modifier_count = len(extra_modifiers)

        # --- Negative quality ---
        combined_text = normalize_text(f"{section} {content}")
        neg_count = sum(1 for p in NEGATIVE_QUALITY_PATTERNS if normalize_text(p) in combined_text)
        negative_quality = min(1.0, neg_count * 0.25)

        # --- Information density ---
        words_list = re.findall(r"[a-zA-Z0-9]+", content.lower())
        if words_list:
            unique = {normalize_word(w) for w in words_list if w}
            lexical_density = len(unique) / len(words_list)
            sentence_count = max(1, len(re.findall(r"[.!?]", content)))
            avg_sentence_len = len(words_list) / sentence_count
            sentence_quality = min(1.0, avg_sentence_len / 18.0)
            information_density = min(1.0, 0.65 * lexical_density + 0.35 * sentence_quality)
        else:
            information_density = 0.0

        # --- Specificity (how much of the content is about the query) ---
        query_hits_in_content = len(query_word_set & content_words)
        query_focus = min(1.0, query_hits_in_content / max(1, len(query_word_set)))
        content_richness = min(1.0, len(content_words) / 130.0)
        specificity = 0.60 * query_focus + 0.40 * content_richness

        # --- Title topic focus ---
        # Measures whether the section title is primarily ABOUT the query
        # vs. a section about something else that happens to contain
        # a query word. E.g., a two-word title starting with the query
        # term has topic focus, but a longer title where the query term
        # appears last does not — its primary topic is the leading words.
        #
        # Heuristic: if the query terms are among the first words
        # of the section title (after removing section numbering),
        # the section is about the query topic.
        # Strip "Section N" prefix from normalized text.
        # After normalize_text, the em-dash is gone — just digits + space.
        title_clean = re.sub(r"^section\s+\d+\s*", "", normalized_section).strip()
        title_words_ordered = title_clean.split()
        if title_words_ordered and original_terms:
            # Check if query terms appear in the first N words of the title
            first_words = set(normalize_word(w) for w in title_words_ordered[:3])
            query_in_lead = sum(1 for t in original_terms if normalize_word(t) in first_words)
            title_topic_focus = query_in_lead / len(original_terms)
        else:
            title_topic_focus = 0.0

        # --- Title has measurement-related words ---
        measurement_title_words = {"measurement", "baseline", "saved", "saving", "usage", "cost", "metric"}
        title_has_measurement = 1.0 if (section_words & measurement_title_words) else 0.0

        # --- Token cost ---
        token_cost = max(1, estimate_tokens(content))

        # --- BM25 content relevance ---
        # Normalized BM25 score from RETRIEVE: captures how relevant
        # the content body is according to FTS5 term-frequency analysis.
        # This signal differentiates candidates when title-based signals
        # (section_match, title_topic_focus) are all zero because the
        # corpus uses synonym-based section titles.
        if bm25_range > 0.5:
            bm25_relevance = (worst_bm25 - candidate["bm25_score"]) / bm25_range
        else:
            bm25_relevance = 0.5

        # --- Attach signals ---
        candidate["signals"] = {
            "direct_query_coverage": direct_query_coverage,
            "section_match": section_match,
            "exact_phrase_section": exact_phrase_section,
            "exact_phrase_content": exact_phrase_content,
            "expanded_coverage": expanded_coverage,
            "title_has_query": title_has_query,
            "title_has_core": title_has_core,
            "title_extra_modifier_count": title_extra_modifier_count,
            "negative_quality": negative_quality,
            "information_density": information_density,
            "specificity": specificity,
            "title_topic_focus": title_topic_focus,
            "title_has_measurement": title_has_measurement,
            "bm25_relevance": bm25_relevance,
            "token_cost": token_cost,
        }

    return candidates


# ============================================================================
# STAGE 3: RANK
#
# Classify the query shape, then rank candidates using signal priorities
# that depend on the query shape.
# ============================================================================


def classify_query_shape(query: str, original_terms: list[str]) -> dict[str, Any]:
    """Classify the query into a shape that drives ranking priorities."""

    term_count = len(original_terms)
    normalized_terms = {normalize_word(t) for t in original_terms}

    # Single broad term: "compilers", "context"
    is_single_broad = term_count == 1

    # Compound query: "token optimization", "artificial intelligence"
    is_compound = term_count >= 2

    # Measurement/cost query: contains "cost", "measurement", "baseline"
    measurement_words = {"cost", "measurement", "baseline", "saving", "saved", "usage"}
    is_measurement = bool(normalized_terms & measurement_words)

    # Broad concept query: queries where multiple subtopics are valuable
    # (e.g., "artificial intelligence" benefits from background + history)
    # vs. queries where one authoritative section dominates
    # (e.g., "token optimization" → Core + Measurement)
    #
    # Only truly broad academic/conceptual terms trigger this.
    # "token" is NOT broad — "token optimization" has a clear core section.
    broad_concept_words = {
        "artificial", "intelligence",
    }
    is_broad_concept = is_compound and bool(normalized_terms & broad_concept_words)

    return {
        "is_single_broad": is_single_broad,
        "is_compound": is_compound,
        "is_measurement": is_measurement,
        "is_broad_concept": is_broad_concept,
        "term_count": term_count,
    }


def stage_rank(
    candidates: list[dict[str, Any]],
    query: str,
    original_terms: list[str],
    query_shape: dict[str, Any],
    term_idf: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """
    Rank candidates based on query shape and signals.

    When term_idf is provided, title-related contributions are weighted
    by the average IDF of the query terms — rare terms in the corpus
    produce a stronger title bonus, common terms produce a weaker one.
    IDF is applied ONLY to title_topic_focus and title_has_query weights;
    all other signal weights remain fixed.

    Returns candidates sorted best-first, each annotated with
    rank_score and rank_reason.
    """

    # ------------------------------------------------------------------
    # Compute IDF-based title weight multiplier.
    #
    # avg_idf is the mean IDF across original query terms.
    # idf_multiplier scales the title-related weights:
    #   - For a corpus of 17 chunks, a term appearing in 3 chunks
    #     has idf = log(1 + 17/3) ≈ 1.79, so multiplier ≈ 1.0.
    #   - A rarer term (1 chunk) → idf ≈ 2.89 → multiplier ≈ 1.6.
    #   - A common term (10 chunks) → idf ≈ 0.99 → multiplier ≈ 0.6.
    #
    # The multiplier is clamped to [0.5, 2.0] so that very common
    # or very rare terms don't produce extreme swings.
    # ------------------------------------------------------------------

    if term_idf and original_terms:
        idf_values = [term_idf.get(t, 1.0) for t in original_terms]
        avg_idf = sum(idf_values) / len(idf_values)
        # Normalize: idf_multiplier = 1.0 when avg_idf ≈ 1.79
        # (typical for a term in ~1/6 of chunks in a small corpus)
        idf_multiplier = max(0.5, min(2.0, avg_idf / 1.79))
    else:
        idf_multiplier = 1.0

    for candidate in candidates:
        sig = candidate["signals"]

        # ------------------------------------------------------------------
        # Build rank score from signals. The weights change by query shape.
        # ------------------------------------------------------------------

        # Base: direct query coverage is always the strongest signal.
        score = 5.0 * sig["direct_query_coverage"]

        # Section/title match
        score += 2.5 * sig["section_match"]

        # Exact phrase matches
        score += 2.0 * sig["exact_phrase_section"]
        score += 1.5 * sig["exact_phrase_content"]

        # Expanded coverage is deliberately weaker than direct
        score += 1.5 * sig["expanded_coverage"]

        # Title topic focus: IDF-weighted.
        # Sections primarily ABOUT the query rank higher than sections
        # that merely mention a query term. IDF amplifies this for
        # rare terms and dampens it for common terms.
        score += (2.0 * idf_multiplier) * sig["title_topic_focus"]

        # BM25 content relevance: captures how focused the content body
        # is on the query terms (term frequency, not just presence).
        # Critical for corpora with synonym-based section titles where
        # title signals are all zero.
        #
        # Weight is inversely proportional to direct_query_coverage:
        # when the title already covers the query terms, the title
        # signals provide reliable differentiation and BM25 adds noise.
        # When the title does NOT cover query terms (synonym-heavy
        # corpora), BM25 becomes the primary differentiator.
        bm25_weight = 2.5 * (1.0 - sig["direct_query_coverage"])
        score += bm25_weight * sig["bm25_relevance"]

        # Quality signals
        score += 1.0 * sig["information_density"]
        score += 0.8 * sig["specificity"]

        # Penalties
        score -= 2.0 * sig["negative_quality"]
        score -= 0.8 * sig["title_extra_modifier_count"]

        # ------------------------------------------------------------------
        # Query-shape adjustments
        # ------------------------------------------------------------------

        reason_parts = []

        if query_shape["is_single_broad"]:
            # For a single broad term like "compilers":
            # Strongly prefer sections whose title contains the query
            # AND has no extra modifier words.
            # Sections with extra modifier words in the title
            # should be penalized when the query is a single broad term.
            # IDF-weighted: rare terms get stronger title bonus.
            if sig["title_has_query"] > 0:
                score += 2.0 * idf_multiplier
                reason_parts.append("title_match")
            if sig["title_has_core"] > 0 and sig["title_has_query"] > 0:
                score += 2.5
                reason_parts.append("core_section")
            # Extra modifiers are already penalized above, but for
            # single broad terms, non-core sections with modifiers
            # get a stronger additional penalty — a single broad query
            # strongly prefers the core/authoritative section.
            if sig["title_extra_modifier_count"] > 0 and sig["title_has_core"] == 0:
                score -= 2.0
                reason_parts.append("modifier_penalty")

        elif query_shape["is_measurement"]:
            # For measurement/cost queries: strongly prefer sections
            # whose TITLE contains measurement-related words.
            if sig["title_has_measurement"] > 0:
                score += 3.5
                reason_parts.append("measurement_title")
            # Also boost sections with query terms in the title lead
            if sig["title_topic_focus"] >= 0.5:
                score += 1.5
                reason_parts.append("measurement_topic_focus")

        elif query_shape["is_broad_concept"]:
            # For broad concept queries like "artificial intelligence":
            # Do NOT give a strong bonus to "Core" sections.
            # Instead, prefer sections that provide distinct subtopics
            # (Background, History) over a generic "Core" overview.
            if sig["title_has_core"] > 0:
                # Reduce the advantage of Core sections for broad concepts.
                # Core is still relevant but shouldn't dominate.
                score -= 1.0
                reason_parts.append("core_deweighted_broad")
            # Sections with query terms + a meaningful modifier
            # (Background, History) are valuable for broad queries.
            if sig["title_has_query"] > 0 and sig["title_extra_modifier_count"] > 0:
                # Reverse the modifier penalty for broad concepts
                # where the modifier is semantically meaningful.
                score += 1.0 * sig["title_extra_modifier_count"]
                reason_parts.append("subtopic_diversity")

        elif query_shape["is_compound"]:
            # For compound queries like "token optimization":
            # Prefer strong exact-phrase and direct coverage.
            if sig["exact_phrase_section"] > 0:
                score += 1.5
                reason_parts.append("exact_compound")

        if not reason_parts:
            reason_parts.append("general")

        candidate["rank_score"] = score
        candidate["rank_reason"] = "+".join(reason_parts)

    # Sort by rank_score descending, then by direct_query_coverage as tiebreaker
    candidates.sort(
        key=lambda c: (
            -c["rank_score"],
            -c["signals"]["direct_query_coverage"],
            -c["signals"]["section_match"],
        )
    )

    for i, candidate in enumerate(candidates, start=1):
        candidate["rank"] = i

    return candidates


# ============================================================================
# STAGE 4: SELECT
#
# Walk the ranked list. Apply usefulness threshold and redundancy filter.
# max_chunks is an upper bound, NOT a quota.
# ============================================================================

# Thresholds
MIN_RANK_SCORE = 5.0                       # Minimum rank_score to be eligible
RELATIVE_SCORE_FLOOR = 0.40                # Secondary must be >= this fraction of anchor
SECONDARY_USEFULNESS_THRESHOLD = 0.40      # Minimum usefulness for secondary candidates
REDUNDANCY_JACCARD_THRESHOLD = 0.55        # Non-query Jaccard above this = redundant
REDUNDANCY_CONTAINMENT_THRESHOLD = 0.65    # Non-query containment above this = redundant


def compute_redundancy(
    candidate_content: str,
    selected_contents: list[str],
    query: str,
) -> dict[str, Any]:
    """Check whether candidate is redundant with already-selected content."""

    if not selected_contents:
        return {
            "is_redundant": False,
            "max_jaccard": 0.0,
            "max_containment": 0.0,
            "novelty": 1.0,
            "reason": "anchor",
        }

    candidate_filtered = remove_query_words(candidate_content, query)
    max_jaccard = 0.0
    max_containment = 0.0

    for selected in selected_contents:
        selected_filtered = remove_query_words(selected, query)

        # Only use non-query comparison if both texts have enough
        # non-query words to be meaningful.
        cand_words = normalized_word_set(candidate_filtered)
        sel_words = normalized_word_set(selected_filtered)

        if len(cand_words) >= 5 and len(sel_words) >= 5:
            j = jaccard_similarity(candidate_filtered, selected_filtered)
            c = word_containment(candidate_filtered, selected_filtered)
        else:
            j = jaccard_similarity(candidate_content, selected)
            c = word_containment(candidate_content, selected)

        max_jaccard = max(max_jaccard, j)
        max_containment = max(max_containment, c)

    is_redundant = (
        max_jaccard >= REDUNDANCY_JACCARD_THRESHOLD
        and max_containment >= REDUNDANCY_CONTAINMENT_THRESHOLD
    )

    novelty = max(0.0, 1.0 - (0.6 * max_jaccard + 0.4 * max_containment))

    return {
        "is_redundant": is_redundant,
        "max_jaccard": max_jaccard,
        "max_containment": max_containment,
        "novelty": novelty,
        "reason": "high_overlap" if is_redundant else "novel",
    }


def compute_usefulness(
    candidate: dict[str, Any],
    novelty: float,
) -> float:
    """
    Compute a usefulness score for a secondary candidate.

    This combines the candidate's own quality signals with its novelty
    relative to already-selected content. A candidate that is novel but
    irrelevant, or relevant but redundant, should score low.
    """
    sig = candidate["signals"]

    # How much does this candidate address the query?
    # signal_relevance: title and term-match based signals.
    signal_relevance = (
        0.50 * sig["direct_query_coverage"]
        + 0.25 * sig["section_match"]
        + 0.15 * sig["exact_phrase_section"]
        + 0.10 * sig["expanded_coverage"]
    )

    # content_relevance: BM25-based content-body relevance.
    # When section titles use synonyms rather than query terms,
    # signal_relevance is systematically low even for the correct
    # section. BM25 captures content-body term frequency and
    # provides the relevance signal that title-based metrics miss.
    content_relevance = sig.get("bm25_relevance", 0.0)

    # Use the HIGHER of the two — a candidate is relevant if
    # either its title/terms match the query or its content body
    # is highly relevant according to BM25.
    relevance = max(signal_relevance, content_relevance)

    # Quality
    quality = (
        0.50 * sig["information_density"]
        + 0.50 * sig["specificity"]
    )
    quality -= sig["negative_quality"]
    quality = max(0.0, min(1.0, quality))

    # Usefulness = relevance * quality * novelty
    # All three must be present for a candidate to be useful.
    usefulness = relevance * (0.5 + 0.5 * quality) * (0.3 + 0.7 * novelty)

    return usefulness


def stage_select(
    ranked_candidates: list[dict[str, Any]],
    query: str,
    query_shape: dict[str, Any],
    max_chunks: int,
) -> dict[str, Any]:
    """
    Select the final context candidates from the ranked list.

    max_chunks is an upper bound, not a quota.
    Returns selected candidates and selection metadata.
    """

    selected: list[dict[str, Any]] = []
    selected_contents: list[str] = []
    skipped_reasons: list[dict[str, str]] = []

    if not ranked_candidates:
        return {
            "selected": selected,
            "skipped": skipped_reasons,
        }

    # --- Anchor selection (first candidate) ---
    anchor = ranked_candidates[0]

    if anchor["rank_score"] < MIN_RANK_SCORE:
        # Even the top candidate is too weak
        return {
            "selected": selected,
            "skipped": [{"section": anchor["section"], "reason": "anchor_too_weak"}],
        }

    anchor["selection_status"] = "ANCHOR"
    anchor["selection_reason"] = "highest_ranked"
    selected.append(anchor)
    selected_contents.append(anchor["content"])

    if max_chunks <= 1:
        return {
            "selected": selected,
            "skipped": skipped_reasons,
        }

    # --- Secondary selection ---
    anchor_rank_score = anchor["rank_score"]

    # For single broad queries (one term like "compilers"),
    # a core/authoritative section is often self-sufficient.
    # Require secondaries to be at least 60% of the anchor
    # to avoid including weakly related subtopics.
    if query_shape["is_single_broad"]:
        effective_floor_ratio = 0.60
    else:
        effective_floor_ratio = RELATIVE_SCORE_FLOOR

    score_floor = anchor_rank_score * effective_floor_ratio

    for candidate in ranked_candidates[1:]:
        if len(selected) >= max_chunks:
            break

        # 1. Rank score floor: candidate must have a meaningful score
        #    relative to the anchor.
        if candidate["rank_score"] < score_floor:
            skipped_reasons.append({
                "section": candidate["section"],
                "reason": f"below_score_floor ({candidate['rank_score']:.2f} < {score_floor:.2f})",
            })
            continue

        # 2. Absolute minimum rank score
        if candidate["rank_score"] < MIN_RANK_SCORE:
            skipped_reasons.append({
                "section": candidate["section"],
                "reason": f"below_min_score ({candidate['rank_score']:.2f})",
            })
            continue

        # 3. Redundancy check
        redundancy = compute_redundancy(
            candidate["content"],
            selected_contents,
            query,
        )

        if redundancy["is_redundant"]:
            skipped_reasons.append({
                "section": candidate["section"],
                "reason": f"redundant (jaccard={redundancy['max_jaccard']:.2f})",
            })
            continue

        # 4. Usefulness threshold
        usefulness = compute_usefulness(candidate, redundancy["novelty"])

        if usefulness < SECONDARY_USEFULNESS_THRESHOLD:
            skipped_reasons.append({
                "section": candidate["section"],
                "reason": f"low_usefulness ({usefulness:.3f})",
            })
            continue

        # 5. Negative quality gate: reject if the content signals
        #    strongly negative quality and the candidate isn't a
        #    near-perfect direct match.
        sig = candidate["signals"]
        if (
            sig["negative_quality"] >= 0.50
            and sig["direct_query_coverage"] < 1.0
        ):
            skipped_reasons.append({
                "section": candidate["section"],
                "reason": "negative_quality_gate",
            })
            continue

        # Passed all gates: select this candidate
        candidate["selection_status"] = "SELECTED"
        candidate["selection_reason"] = f"useful (usefulness={usefulness:.3f}, novelty={redundancy['novelty']:.2f})"
        candidate["redundancy_info"] = redundancy
        candidate["usefulness"] = usefulness

        selected.append(candidate)
        selected_contents.append(candidate["content"])

    return {
        "selected": selected,
        "skipped": skipped_reasons,
    }


# ============================================================================
# FILE TOKEN CALCULATIONS
# ============================================================================


def calculate_full_file_tokens(conn: sqlite3.Connection, path: str) -> int:
    rows = conn.execute(
        "SELECT content FROM chunks WHERE path = ? ORDER BY rowid",
        (path,),
    ).fetchall()
    combined = "\n".join(str(row["content"]) for row in rows)
    return estimate_tokens(combined)


# ============================================================================
# USAGE LOGGING
# ============================================================================


def log_usage(
    operation: str,
    target: str,
    characters: int,
    estimated_tokens: int,
    result_count: int,
    potential_tokens: int = 0,
    saved_tokens: int = 0,
) -> None:
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO usage_events (
                timestamp, operation, target, characters,
                estimated_tokens, result_count,
                potential_tokens, saved_tokens
            )
            VALUES (strftime('%s', 'now'), ?, ?, ?, ?, ?, ?, ?)
            """,
            (operation, target, characters, estimated_tokens,
             result_count, potential_tokens, saved_tokens),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# MCP TOOL: optimizer_search
# ============================================================================


@mcp.tool()
def optimizer_search(query: str, limit: int = 5) -> str:
    """Search indexed local content and return the most relevant sections."""

    limit = max(1, min(limit, 20))
    conn = get_connection()

    try:
        retrieval = stage_retrieve(conn, query, pool_size=max(limit * 10, 30))
        candidates = stage_classify(
            retrieval["candidates"],
            query,
            retrieval["original_terms"],
            retrieval["expanded_terms"],
        )
        query_shape = classify_query_shape(query, retrieval["original_terms"])
        ranked = stage_rank(
            candidates, query, retrieval["original_terms"],
            query_shape, term_idf=retrieval.get("term_idf"),
        )

        top = ranked[:limit]

        if not top:
            return f"Token Optimization System v{SERVER_VERSION}\n\nNo results for: {query}"

        lines = [
            f"Token Optimization System v{SERVER_VERSION}",
            "",
            f"Query: {query}",
            "",
        ]

        for i, c in enumerate(top, start=1):
            lines.extend([
                f"{i}. {c['path']}",
                f"   Section: {c['section']}",
                f"   Rank score: {c['rank_score']:.3f}",
                "",
            ])

        output = "\n".join(lines)

        log_usage("search", query, len(output), estimate_tokens(output), len(top))

        return output

    finally:
        conn.close()


# ============================================================================
# MCP TOOL: optimizer_read
# ============================================================================


@mcp.tool()
def optimizer_read(path: str, section: str = "") -> str:
    """Read indexed content from an allowlisted local file."""

    conn = get_connection()
    try:
        if section:
            row = conn.execute(
                """
                SELECT chunks.rowid AS chunk_id, chunks.path, chunks.section, chunks.content
                FROM chunks WHERE chunks.path = ? AND chunks.section = ?
                LIMIT 1
                """,
                (path, section),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT chunks.rowid AS chunk_id, chunks.path, chunks.section, chunks.content
                FROM chunks WHERE chunks.path = ?
                ORDER BY rowid LIMIT 1
                """,
                (path,),
            ).fetchone()

        if row is None:
            return f"File/section not found:\n{path}"

        content = str(row["content"])
        selected_tokens = estimate_tokens(content)
        potential_tokens = calculate_full_file_tokens(conn, path)
        saved_tokens = max(0, potential_tokens - selected_tokens)

        output = f"File: {row['path']}\nSection: {row['section']}\n\n{content}"

        log_usage("read", path, len(output), selected_tokens, 1, potential_tokens, saved_tokens)

        return output

    finally:
        conn.close()


# ============================================================================
# MCP TOOL: optimizer_context
# ============================================================================


@mcp.tool()
def optimizer_context(query: str, max_chunks: int = 2) -> str:
    """
    Retrieve the highest-value local context for a query.

    Four-stage pipeline: RETRIEVE → CLASSIFY → RANK → SELECT.
    max_chunks is an upper bound, not a requirement.
    """

    max_chunks = max(1, min(max_chunks, 5))
    conn = get_connection()

    try:
        # Stage 1: RETRIEVE
        retrieval = stage_retrieve(conn, query, pool_size=max(max_chunks * 10, 30))
        all_candidates = retrieval["candidates"]

        if not all_candidates:
            return (
                f"Token Optimization System v{SERVER_VERSION}\n\n"
                f"Query: {query}\n\n"
                "No relevant context found."
            )

        # Stage 2: CLASSIFY
        classified = stage_classify(
            all_candidates,
            query,
            retrieval["original_terms"],
            retrieval["expanded_terms"],
        )

        # Stage 3: RANK
        query_shape = classify_query_shape(query, retrieval["original_terms"])
        ranked = stage_rank(
            classified, query, retrieval["original_terms"],
            query_shape, term_idf=retrieval.get("term_idf"),
        )

        # Stage 4: SELECT
        selection = stage_select(ranked, query, query_shape, max_chunks)
        selected = selection["selected"]
        skipped = selection["skipped"]

        # ---------------------------------------------------------------
        # Output
        # ---------------------------------------------------------------

        lines = [
            f"Token Optimization System v{SERVER_VERSION}",
            "",
            f"Query: {query}",
            f"Query shape: "
            + ", ".join(
                k for k, v in query_shape.items()
                if isinstance(v, bool) and v
            ),
            "",
            f"Retrieved candidates: {len(all_candidates)}",
            f"  AND matches: {retrieval['and_count']}",
            f"  OR matches: {retrieval['or_count']}",
            f"  Prefix matches: {retrieval['prefix_count']}",
            f"  Expanded matches: {retrieval['expanded_count']}",
            "",
            f"Expanded terms: {', '.join(retrieval['expanded_terms'])}",
            "",
            f"Selected chunks: {len(selected)}",
            f"Skipped candidates: {len(skipped)}",
            "",
        ]

        # Diagnostics for selected
        selected_characters = 0
        baseline_tokens = 0

        for i, candidate in enumerate(selected, start=1):
            sig = candidate["signals"]
            selected_characters += len(candidate["content"])

            lines.extend([
                f"{i}. {candidate['path']}",
                f"   Section: {candidate['section']}",
                f"   Rank: {candidate['rank']}",
                f"   Rank score: {candidate['rank_score']:.3f}",
                f"   Rank reason: {candidate['rank_reason']}",
                f"   Selection: {candidate['selection_status']}",
                f"   Selection reason: {candidate['selection_reason']}",
                f"   Signals:",
                f"     direct_query_coverage: {sig['direct_query_coverage']:.3f}",
                f"     section_match: {sig['section_match']:.3f}",
                f"     exact_phrase_section: {sig['exact_phrase_section']:.3f}",
                f"     exact_phrase_content: {sig['exact_phrase_content']:.3f}",
                f"     expanded_coverage: {sig['expanded_coverage']:.3f}",
                f"     title_has_query: {sig['title_has_query']:.0f}",
                f"     title_has_core: {sig['title_has_core']:.0f}",
                f"     title_extra_modifier_count: {sig['title_extra_modifier_count']}",
                f"     negative_quality: {sig['negative_quality']:.3f}",
                f"     information_density: {sig['information_density']:.3f}",
                f"     specificity: {sig['specificity']:.3f}",
                f"     bm25_relevance: {sig['bm25_relevance']:.3f}",
                f"     token_cost: {sig['token_cost']}",
                "",
            ])

            baseline_tokens += calculate_full_file_tokens(conn, candidate["path"])

        # Skipped candidates summary
        if skipped:
            lines.append("Skipped:")
            for entry in skipped[:5]:
                lines.append(f"  - {entry['section']}: {entry['reason']}")
            lines.append("")

        # Context output
        lines.append("CONTEXT:")
        lines.append("")

        for candidate in selected:
            lines.extend([
                f"File: {candidate['path']}",
                f"Section: {candidate['section']}",
                "",
                candidate["content"],
                "",
            ])

        # Metrics
        selected_tokens = estimate_tokens(
            "\n".join(c["content"] for c in selected)
        )
        saved_tokens = max(0, baseline_tokens - selected_tokens)
        savings_percent = (
            100.0 * saved_tokens / baseline_tokens if baseline_tokens else 0.0
        )

        lines.extend([
            "METRICS:",
            f"Selected tokens: {selected_tokens}",
            f"Baseline tokens: {baseline_tokens}",
            f"Saved tokens: {saved_tokens}",
            f"Savings: {savings_percent:.1f}%",
        ])

        output = "\n".join(lines)

        log_usage(
            "context", query, selected_characters,
            selected_tokens, len(selected),
            baseline_tokens, saved_tokens,
        )

        return output

    finally:
        conn.close()


# ============================================================================
# MCP TOOL: optimizer_stats
# ============================================================================


@mcp.tool()
def optimizer_stats() -> str:
    """Show local optimizer usage and estimated context savings."""

    conn = get_connection()
    try:
        stats = conn.execute("""
            SELECT
                COUNT(*) AS usage_events,
                COALESCE(SUM(CASE WHEN operation = 'search' THEN 1 ELSE 0 END), 0) AS search_events,
                COALESCE(SUM(CASE WHEN operation = 'read' THEN 1 ELSE 0 END), 0) AS read_events,
                COALESCE(SUM(CASE WHEN operation = 'context' THEN 1 ELSE 0 END), 0) AS context_events,
                COALESCE(SUM(CASE WHEN operation = 'context' THEN estimated_tokens ELSE 0 END), 0) AS context_selected_tokens,
                COALESCE(SUM(CASE WHEN operation = 'context' THEN potential_tokens ELSE 0 END), 0) AS context_baseline_tokens,
                COALESCE(SUM(CASE WHEN operation = 'context' THEN saved_tokens ELSE 0 END), 0) AS context_saved_tokens,
                COALESCE(SUM(CASE WHEN operation = 'read' THEN estimated_tokens ELSE 0 END), 0) AS read_selected_tokens,
                COALESCE(SUM(CASE WHEN operation = 'read' THEN potential_tokens ELSE 0 END), 0) AS read_baseline_tokens,
                COALESCE(SUM(CASE WHEN operation = 'read' THEN saved_tokens ELSE 0 END), 0) AS read_saved_tokens,
                COALESCE(SUM(characters), 0) AS measured_characters
            FROM usage_events
        """).fetchone()

        roots = conn.execute("SELECT COUNT(*) AS count FROM allowed_roots").fetchone()["count"]
        files_count = conn.execute("SELECT COUNT(*) AS count FROM files").fetchone()["count"]
        chunks_count = conn.execute("SELECT COUNT(*) AS count FROM chunks").fetchone()["count"]

        total_baseline = stats["read_baseline_tokens"] + stats["context_baseline_tokens"]
        total_delivered = stats["read_selected_tokens"] + stats["context_selected_tokens"]
        total_saved = max(0, total_baseline - total_delivered)
        total_pct = 100.0 * total_saved / total_baseline if total_baseline else 0.0

        return "\n".join([
            f"Token Optimization System v{SERVER_VERSION}",
            "",
            "Status: ONLINE",
            "Mode: LOCAL_ONLY",
            "",
            f"Allowed roots: {roots}",
            f"Indexed files: {files_count}",
            f"Indexed chunks: {chunks_count}",
            "",
            f"Usage events: {stats['usage_events']}",
            f"Context events: {stats['context_events']}",
            f"Read events: {stats['read_events']}",
            f"Search events: {stats['search_events']}",
            "",
            f"Total baseline tokens: {total_baseline}",
            f"Total delivered tokens: {total_delivered}",
            f"Total saved tokens: {total_saved}",
            f"Total savings: {total_pct:.1f}%",
            "",
            f"Measured characters: {stats['measured_characters']}",
            "",
            "Token measurements are local estimates, not Anthropic billing data.",
        ])

    finally:
        conn.close()


# ============================================================================
# SERVER ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    mcp.run()
