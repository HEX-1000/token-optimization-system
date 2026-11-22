"""S at a compaction window: the B1 rule of the private tool context_profile_a2.py.

S(W) = 1 - (sum of the context read when a compaction fires whenever the context would pass W) /
(sum of the context actually read). The simulation runs over the counted calls of each session file
in call order. A real compaction in the log clears the offset; a fall of the context that is not a
real compaction lowers the offset by the size of the fall (never below 0).

AFTER is the context right after a compaction, fixed from the old logs (median of 9). The same value
is used at every window: what is left after a compaction (prefix, summary, kept records) does not
depend on the window. Sums are stored, not shares, so sessions can be pooled.
"""
LIMIT = 200_000
AFTER = 73_710
WINDOWS = (200_000, 400_000)


def counted_calls(ses):
    """(context, real compaction) of the counted calls of an analyzed session, in call order."""
    steps = ses.an.steps
    return [(c.context, steps[k].kind == "compaction") for k, c in enumerate(ses.an.fd.calls) if c.counted]


def simulate_b1(seq, limit=LIMIT, after=AFTER):
    """Sum read with compaction at `limit`, number of calls with a negative effective context, falls.

    seq is [(context, real compaction)]. A fall is (index in seq, size, offset before it, offset removed)."""
    offset, added, negative, prev, falls = 0, 0, 0, None, []
    for i, (c, real) in enumerate(seq):
        if real:
            offset = 0
        elif prev is not None and c < prev:
            removed = min(offset, prev - c)
            falls.append((i, prev - c, offset, removed))
            offset -= removed
        eff = c - offset
        if eff < 0:
            negative += 1
        if eff > limit:
            added += eff
            offset += eff - after
            eff = after
        added += eff
        prev = c
    return added, negative, falls


def window_sums(ses, windows=WINDOWS):
    """{window: [sum of C, sum with compaction at the window]} for one analyzed session."""
    seq = counted_calls(ses)
    sum_c = sum(c for c, _ in seq)
    return {w: [sum_c, simulate_b1(seq, limit=w)[0]] for w in windows}


def s_value(sum_c, added):
    """S from pooled sums; 0.0 when nothing was read."""
    return 1.0 - added / sum_c if sum_c else 0.0
