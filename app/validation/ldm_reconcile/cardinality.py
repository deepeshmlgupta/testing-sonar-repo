"""
Cardinality & Relationship Signatures
-------------------------------------
Cardinality is the single highest-value thing a conceptual model carries, and
the two tools express it in completely different vocabularies:

    PowerDesigner CDM   "0,1"  "1,1"  "0,n"  "1,n"      (per relationship end)
    erwin logical       "Zero, One or More"
                        "One or More (P)"
                        "Zero or One (Z)"
                        "Exactly n"
                        + Nulls_Allowed / Relationship_Type for optionality

Everything is folded onto the PowerDesigner notation, which is the more precise
of the two, and then onto a *degree* (1:1, 1:N, N:1, M:N) for reporting.

Relationship identity is also solved here.  A conceptual relationship has no
columns to key on, so it is identified by its endpoint pair — which means the
signature must be direction-normalised: swapping the two ends must also swap
the two cardinalities, or an otherwise identical relationship read in the
opposite order would look like a defect.
"""

import re
from typing import Tuple

from .ldm_model import Relationship, RelationshipEnd
from .normalizers import compare_key, normalize_name

# ─── CANONICAL FORMS ──────────────────────────────────────────────────────────
ZERO_ONE  = "0,1"    # optional, at most one
ONE_ONE   = "1,1"    # mandatory, exactly one
ZERO_MANY = "0,n"    # optional, many
ONE_MANY  = "1,n"    # mandatory, many

_VALID = {ZERO_ONE, ONE_ONE, ZERO_MANY, ONE_MANY}

# Every spelling either tool is known to emit, folded onto the canonical form.
_CARDINALITY_ALIASES = {
    # PowerDesigner native
    "0,1": ZERO_ONE, "1,1": ONE_ONE, "0,n": ZERO_MANY, "1,n": ONE_MANY,
    "0..1": ZERO_ONE, "1..1": ONE_ONE, "0..n": ZERO_MANY, "1..n": ONE_MANY,
    "0..*": ZERO_MANY, "1..*": ONE_MANY, "*": ZERO_MANY,

    # erwin verbal forms
    "zero, one or more":  ZERO_MANY,
    "zero one or more":   ZERO_MANY,
    "zeroonemore":        ZERO_MANY,
    "zeroormore":         ZERO_MANY,
    "zero or more":       ZERO_MANY,
    "one or more":        ONE_MANY,
    "one or more (p)":    ONE_MANY,
    "oneormore":          ONE_MANY,
    "p":                  ONE_MANY,
    "zero or one":        ZERO_ONE,
    "zero or one (z)":    ZERO_ONE,
    "zeroorone":          ZERO_ONE,
    "z":                  ZERO_ONE,
    "one":                ONE_ONE,
    "exactly one":        ONE_ONE,
    "exactlyone":         ONE_ONE,
    "many":               ZERO_MANY,
    "many to many":       ZERO_MANY,
    "optional":           ZERO_ONE,
    "mandatory":          ONE_ONE,
}

_EXACTLY_N = re.compile(r"^exactly\s*(\d+)$", re.IGNORECASE)

# Words that mean "the upper bound is unbounded" and "the lower bound is zero".
# erwin's four cardinality options are closed set of English phrases, so keying
# on their content words covers every punctuation/spacing variant its exporter
# and its GUI produce, instead of enumerating exact strings.
_UNBOUNDED_WORDS = {"more", "many", "n", "m"}
_OPTIONAL_WORDS  = {"zero", "optional", "most"}   # "most" for "at most one"
_CARDINALITY_WORDS = (_UNBOUNDED_WORDS | _OPTIONAL_WORDS
                      | {"one", "exactly", "mandatory", "or", "least"})


def _phrase_bounds(token: str) -> str:
    """
    Interpret a verbal cardinality phrase by its content words.

    Returns a canonical form, or "" when the token carries no cardinality
    vocabulary (in which case the caller should try numeric parsing).

        "Zero, One or More"  -> 0,n      "Zero,One or More"   -> 0,n
        "One or More (P)"    -> 1,n      "Zero or One (Z)"    -> 0,1
    """
    words = set(re.findall(r"[a-z]+", token))
    if not words & _CARDINALITY_WORDS:
        return ""
    # A bare "n"/"m" is a placeholder, not a word; catch it via the raw token.
    unbounded = bool(words & _UNBOUNDED_WORDS) or "*" in token
    optional  = bool(words & _OPTIONAL_WORDS)
    low  = "0" if optional else "1"
    high = "n" if unbounded else "1"
    return f"{low},{high}"

# Degree labels for the report
DEGREE_ONE_ONE   = "1:1"
DEGREE_ONE_MANY  = "1:N"
DEGREE_MANY_ONE  = "N:1"
DEGREE_MANY_MANY = "M:N"


# ─── NORMALISATION ────────────────────────────────────────────────────────────

def _compact_token(token: str) -> str:
    """
    Tolerate "0 , n" / "0-n" / "(0,n)" and similar hand-edited spellings.

    The hyphen is a RANGE separator only between two tokens ("0-n"); a leading
    "-" is a sign.  Blanket-replacing it turned erwin's "-3" into ",3", which
    the positional parser then read as low="" high="3" -> "0,1".
    """
    compact = re.sub(r"[\s()\[\]]", "", token)
    compact = re.sub(r"(?<=[0-9A-Za-z*])-(?=[0-9A-Za-z*])", ",", compact)
    return compact.replace("..", ",")


def _numeric_pair(compact: str) -> str:
    """
    Positional low,high parsing for genuinely numeric spellings ("0,n", "1,*").

    Returns a canonical form, or "" when the token is not a two-part pair
    carrying at least one digit or star.
    """
    parts = compact.split(",")
    if len(parts) != 2 or not re.search(r"[\d*]", compact):
        return ""
    low, high = parts[0], parts[1]
    low_token  = "1" if low in ("1", "one") else "0"
    high_token = "n" if high in ("n", "m", "*", "many", "more") else "1"
    candidate = f"{low_token},{high_token}"
    return candidate if candidate in _VALID else ""


def _from_flags(mandatory, many) -> str:
    """
    Fallback for erwin exports that carry only boolean-ish flags
    (Nulls_Allowed, Relationship_Type) rather than a phrase.  "" when neither
    flag was supplied, so nothing is invented.
    """
    if mandatory is None and many is None:
        return ""
    low_token  = "1" if mandatory else "0"
    high_token = "n" if many else "1"
    return f"{low_token},{high_token}"


def normalize_cardinality(raw: str,
                          mandatory: bool = None,
                          many: bool = None) -> str:
    """
    Fold any cardinality spelling onto "0,1" | "1,1" | "0,n" | "1,n".

    ``mandatory`` and ``many`` are fallbacks for erwin exports that carry only
    boolean-ish flags (Nulls_Allowed, Relationship_Type) rather than a phrase.
    An unparseable value returns "" so the comparator can report "not stated"
    instead of inventing a cardinality that was never modelled.
    """
    token = (raw or "").strip().lower()

    if token in _CARDINALITY_ALIASES:
        return _CARDINALITY_ALIASES[token]

    exact = _EXACTLY_N.match(token)
    if exact:
        return ONE_ONE if exact.group(1) == "1" else ONE_MANY

    compact = _compact_token(token)
    if compact in _CARDINALITY_ALIASES:
        return _CARDINALITY_ALIASES[compact]

    # ── Verbal phrases BEFORE numeric-pair parsing ───────────────────────────
    # erwin stores cardinality as an English display phrase ("Zero, One or
    # More"), which is itself comma-bearing.  Handing such a phrase to the
    # positional low,high parser silently yields a *valid-looking* but wrong
    # pair ("zero,one or more" -> low="zero", high="oneormore" -> 0,1),
    # collapsing an unbounded end to at-most-one.  So interpret the words
    # semantically first and only fall through to positional parsing for
    # genuinely numeric spellings.
    phrase = _phrase_bounds(token)
    if phrase:
        return phrase

    pair = _numeric_pair(compact)
    if pair:
        return pair

    return _from_flags(mandatory, many)


def is_mandatory(cardinality: str) -> bool:
    """True when the lower bound is 1."""
    return normalize_cardinality(cardinality).startswith("1")


def is_many(cardinality: str) -> bool:
    """True when the upper bound is n."""
    return normalize_cardinality(cardinality).endswith("n")


def describe_cardinality(cardinality: str) -> str:
    """Plain-English rendering for a report cell."""
    canonical = normalize_cardinality(cardinality)
    return {
        ZERO_ONE:  "0,1 (optional, one)",
        ONE_ONE:   "1,1 (mandatory, one)",
        ZERO_MANY: "0,n (optional, many)",
        ONE_MANY:  "1,n (mandatory, many)",
    }.get(canonical, "(not stated)")


def degree(end1_cardinality: str, end2_cardinality: str) -> str:
    """
    Relationship degree from the two end cardinalities.

    Note the PowerDesigner convention: the cardinality stored on end1 describes
    how many *end2* instances relate to one end1 instance.  The degree is
    therefore read from the pair, not from either end alone.
    """
    many1 = is_many(end1_cardinality)
    many2 = is_many(end2_cardinality)
    if many1 and many2:
        return DEGREE_MANY_MANY
    if many1:
        return DEGREE_ONE_MANY
    if many2:
        return DEGREE_MANY_ONE
    return DEGREE_ONE_ONE


def invert_degree(value: str) -> str:
    """Mirror a degree label, used when a relationship is read end-for-end."""
    return {DEGREE_ONE_MANY: DEGREE_MANY_ONE,
            DEGREE_MANY_ONE: DEGREE_ONE_MANY}.get(value, value)


# ─── RELATIONSHIP SIGNATURES ──────────────────────────────────────────────────

def _oriented_ends(rel: Relationship) -> Tuple[RelationshipEnd, RelationshipEnd, bool]:
    """
    Return the two ends in a stable alphabetical order plus a flag saying whether
    they were swapped.  Ordering by entity code makes the signature independent
    of the order in which the source tool happened to serialise the ends.
    """
    key1 = compare_key(rel.end1.entity)
    key2 = compare_key(rel.end2.entity)
    if key1 <= key2:
        return rel.end1, rel.end2, False
    return rel.end2, rel.end1, True


def endpoint_signature(rel: Relationship) -> str:
    """
    Endpoint-only signature: identifies *which two entities* are related,
    ignoring how.  Used for the second matching pass, where a relationship is
    recognised as the same relationship whose cardinality then differs.
    """
    left, right, _ = _oriented_ends(rel)
    return f"{normalize_name(left.entity)}::{normalize_name(right.entity)}"


def full_signature(rel: Relationship) -> str:
    """
    Endpoint + cardinality signature.  Direction-normalised: when the ends are
    swapped, their cardinalities travel with them.
    """
    left, right, _ = _oriented_ends(rel)
    left_card  = normalize_cardinality(left.cardinality)  or "?"
    right_card = normalize_cardinality(right.cardinality) or "?"
    return (f"{normalize_name(left.entity)}[{left_card}]"
            f"::{normalize_name(right.entity)}[{right_card}]")


_DISAMBIGUATOR = re.compile(r"/\d+$")


def strip_disambiguator(label: str) -> str:
    """
    Remove erwin's duplicate-name suffix.

    erwin requires relationship names to be unique and appends "/1", "/2", ... to
    later duplicates.  PowerDesigner has no such rule and leaves every duplicate
    with the same name, so "origin for" in the CDM arrives as "origin for" and
    "origin for/1" in erwin.  Comparing the raw names makes the second one look
    like a deleted relationship plus an unrelated new one.
    """
    return _DISAMBIGUATOR.sub("", (label or "").strip())


def name_signature(rel: Relationship) -> str:
    """
    Name-based signature for the first matching pass.  Relationship names are
    the most reliable anchor when the migration preserved them, and worthless
    when it did not — hence it is only ever one pass of several.
    """
    label = rel.code or rel.name
    return normalize_name(strip_disambiguator(label))


def name_endpoint_signature(rel: Relationship) -> str:
    """
    Name AND endpoints together.

    Needed because either key alone can be ambiguous while the pair is unique:
    a CDM model may hold two relationships called "origin for", and two distinct
    relationships ("origin for", "destination for") may join the very same two
    entities.  match_objects only pairs keys unique on both sides, so with name
    and endpoints as separate passes both are skipped and the relationships are
    reported as missing AND extra — the same objects counted twice.
    """
    return f"{name_signature(rel)}##{endpoint_signature(rel)}"


def describe_relationship(rel: Relationship) -> str:
    """
    One-line human rendering, e.g.
        "CUSTOMER (0,n) places → (1,1) is placed by SALES_ORDER  [1:N]"
    """
    end1, end2 = rel.end1, rel.end2
    role1 = end1.role or "relates to"
    role2 = end2.role or "relates to"
    return (
        f"{end1.entity} ({normalize_cardinality(end1.cardinality) or '?'}) {role1} → "
        f"({normalize_cardinality(end2.cardinality) or '?'}) {role2} {end2.entity} "
        f"[{degree(end1.cardinality, end2.cardinality)}]"
    )


def ends_aligned(pd_rel: Relationship, erwin_rel: Relationship) -> bool:
    """
    True when both relationships list their ends in the same order.  When False,
    the comparator must compare pd.end1 against erwin.end2 and vice-versa before
    reporting a role-name or optionality difference.

    Ordering is normally decided by entity name, but a SELF-REFERENCING
    relationship has the same entity at both ends ("LOCATION sub-location
    LOCATION"), so the name carries no ordering information at all and
    _oriented_ends reports "not swapped" for both sides even when the two tools
    serialised the ends in opposite order.  For those, fall back to the shape of
    the relationship — which end is the "many" side — and then to which end
    carries the role name.
    """
    pd_recursive    = compare_key(pd_rel.end1.entity) == compare_key(pd_rel.end2.entity)
    erwin_recursive = compare_key(erwin_rel.end1.entity) == compare_key(erwin_rel.end2.entity)

    if pd_recursive or erwin_recursive:
        pd_many    = (is_many(pd_rel.end1.cardinality),
                      is_many(pd_rel.end2.cardinality))
        erwin_many = (is_many(erwin_rel.end1.cardinality),
                      is_many(erwin_rel.end2.cardinality))
        if pd_many != erwin_many and pd_many == tuple(reversed(erwin_many)):
            return False
        if pd_many == erwin_many and pd_many[0] != pd_many[1]:
            return True
        # Multiplicity is symmetric (1:1 or M:N) — use the role name instead.
        pd_role    = (bool(pd_rel.end1.role), bool(pd_rel.end2.role))
        erwin_role = (bool(erwin_rel.end1.role), bool(erwin_rel.end2.role))
        if pd_role != erwin_role and pd_role == tuple(reversed(erwin_role)):
            return False
        return True

    _, _, pd_swapped    = _oriented_ends(pd_rel)
    _, _, erwin_swapped = _oriented_ends(erwin_rel)
    return pd_swapped == erwin_swapped
