# How it works

This page describes release 0.25.2 (retrieval V0.25.0 with compact output).

## Indexing

`indexer.py` walks a folder you choose and splits each supported file into sections:

| File type | Split by |
|---|---|
| Markdown | headings |
| Word (.docx) | headings |
| PDF | page |
| PowerPoint (.pptx) | slide |
| Excel (.xlsx) | sheet |
| Other text files | not split: one section per file |

Sections go into an SQLite FTS5 table with the `unicode61` tokenizer: lowercase, Unicode word splitting, no stemming, no stop words. A file is read again only when its content hash changes.

## Answering a question (`optimizer_context`)

1. **Normalize.** The question is lowercased and reduced to `a-z`, `0-9` and spaces; a small list of expansions adds related terms.
2. **Retrieve.** Four full-text queries collect candidates: all terms, any term, term prefixes, and expanded prefixes. A candidate keeps its best BM25 score across the four.
3. **Classify.** For each candidate: how many question terms appear in its heading and text (coverage), phrase matches, and BM25 relevance scaled within the candidate pool.
4. **Rank.** A weighted score combines coverage, heading match, BM25 relevance, a quality estimate and penalties.
5. **Select.** The top candidate is returned only if its score is at least 5.0; otherwise the answer is "no context". Each further section must score at least 40% of the top one, must not repeat it, and must pass a usefulness check. `max_chunks` (1 to 5, default 2) is an upper bound, not a target.
6. **Return.** The compact format sends each selected section under a short file label, followed by the names of matching sections that were left out. `debug=True` returns the full diagnostic output instead, at about twice the tokens.

## How changes are evaluated

Every change to retrieval is compared with the frozen baseline on a fixed evaluation suite of 89 questions (79 with required sections, 10 where the right answer is "nothing relevant"). Each question is checked on its own: required sections found, irrelevant sections returned, correct abstention, and tokens delivered. One improved number does not justify a change that makes another question worse.

Baseline V0.25.0 on that suite: all required sections found on 65 of 79 questions, 24 irrelevant sections returned, correct abstention on 6 of 10, and 24,546 tokens in compact format against 55,710 in the original output format.

Ten later changes to tokenization, stop words, path matching, BM25 weighting and selection were tested this way. None was accepted yet, because each one that helped some questions hurt others.
