"""Reconcile OpenRouter rankings slugs with OpenRouter price-list slugs.

The rankings-daily dataset (data/tokens_by_model.csv) names models with a
release tag, e.g. ``deepseek/deepseek-v4.1-flash-20261017`` or
``minimax/minimax-m3.1-flash-20261019:free``. The models endpoint
(data/model_prices.csv) lists the same model as the bare slug
``deepseek/deepseek-v4.1-flash`` or, for dated re-releases, with a
four-digit month-day tag ``deepseek/deepseek-v4.1-flash-1017``. Joining
the two on the raw string therefore matched only a few percent of token
volume by late 2026 (it was ~25% in 2025, when fewer slugs were dated).

``resolve()`` maps each rankings slug to the first price-list slug that
exists, trying the most specific form first:

    1. the slug as-is
    2. ``name-MMDD[:suffix]``   (dated re-release with its own price)
    3. ``name[:suffix]``        (the bare model)

A ``:free`` (or any other) suffix is preserved through every candidate
and never dropped: a free-tier endpoint must not inherit the paid price.
Slugs that resolve to nothing are returned unchanged, so they still fail
the join exactly as before.
"""
import re

_DATED = re.compile(r"^(?P<base>.+?)-(?P<yyyy>\d{4})(?P<mmdd>\d{4})(?P<sfx>:[\w-]+)?$")


def candidates(slug):
    """Ordered list of price-list slugs that could price this rankings slug."""
    out = [slug]
    m = _DATED.match(slug)
    if m:
        base, mmdd, sfx = m.group("base"), m.group("mmdd"), m.group("sfx") or ""
        out.append(f"{base}-{mmdd}{sfx}")
        out.append(f"{base}{sfx}")
    return out


def resolve(models, price_models):
    """Map an iterable/Series of rankings slugs onto price-list slugs.

    ``price_models`` is the set of slugs present in model_prices.csv.
    Returns a list (or Series, if given one) of resolved slugs; unmatched
    slugs pass through unchanged.
    """
    price_models = set(price_models)
    cache = {}

    def one(s):
        if s not in cache:
            cache[s] = next((c for c in candidates(s) if c in price_models), s)
        return cache[s]

    if hasattr(models, "map"):
        return models.map(one)
    return [one(s) for s in models]


def report(tok, price_models, label):
    """Print what share of token volume now joins to a price row."""
    resolved = resolve(tok["model"], price_models)
    hit = resolved.isin(set(price_models))
    tot = tok["tokens"].sum()
    share = tok.loc[hit, "tokens"].sum() / tot if tot else 0
    print(f"[{label}] price join: {hit.sum()}/{len(tok)} model-days, "
          f"{share:.0%} of token volume priced")
    return resolved
