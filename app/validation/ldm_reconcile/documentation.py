"""
documentation.py — Comments → Notes and Definition → Definition mapping
=======================================================================

Builds the rows behind the LDM report's DESCRIPTION sheet.

WHY THIS IS SEPARATE FROM THE COMPARATOR
----------------------------------------
The comparator's DEFINITION check reads a single ``definition`` string that
each parser resolves by first-match-wins across several source fields.  That
behaviour is deliberately left untouched here so every existing LDM finding,
the fidelity score and the PASS/WARN/FAIL status stay exactly as they were.

This module answers a different question, one the findings list cannot: for
every object, *which specific field* holds the documentation on each side, did
it survive the migration, and what does each side actually say?  It reports
matches as well as mismatches — a findings list only ever shows what went
wrong, so there was previously no way to tell "verified identical" apart from
"never checked".

THE THREE MAPPINGS
------------------
    SAP PD <a:Comment>      →  erwin Note_List_Array/Note_List  (field 6)
    SAP PD <a:Description>  →  erwin <Definition>
    SAP PD <a:Annotation>   →  erwin Extended_Notes  (…/Extended_NotesProps/Comment)

These are the pairings confirmed in the two tools' UIs, and each is validated
against the field the text is actually visible in.

The first is the pairing the framework's preprocessing stage creates:
``app/preprocessing/scripts/pd_comment_to_erwin_note.py`` writes each PD
Comment into an erwin Note.  Reporting on it is how you confirm preprocessing
actually landed, per object, rather than trusting the run log.  erwin's own
Comment field also receives the PD Comment during import, but Notes is the
carrier the erwin UI shows, so Notes is what is validated here.

The last two are the two sub-tabs of PowerDesigner's Definition tab, and they
land in DIFFERENT erwin fields.  They are reported separately because folding
Annotation into Description (the parser's previous fallback) labelled an
Annotation as a Description and then reported it lost, when erwin had in fact
carried it correctly into Extended Notes.

STATUS VALUES
-------------
    MATCHED             both sides carry equivalent text
    MISMATCH            both sides populated, text differs
    MISSING_IN_ERWIN    SAP PD has text, erwin does not  ← migration loss
    MISSING_IN_SAP_PD   erwin has text, SAP PD does not  ← added downstream
    BOTH_EMPTY          neither side documents the object

Equivalence uses the same ``normalizers.definitions_match`` the comparator
uses, so this sheet and the DEFINITION findings agree on what "same" means.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import normalizers

# ─── STATUS CONSTANTS ─────────────────────────────────────────────────────────
MATCHED = "MATCHED"
MISMATCH = "MISMATCH"
MISSING_IN_ERWIN = "MISSING_IN_ERWIN"
MISSING_IN_SAP_PD = "MISSING_IN_SAP_PD"
BOTH_EMPTY = "BOTH_EMPTY"

# Mapping identifiers, used as the "Mapping" column value.
# Longest text written into a report cell before truncation.
VALUE_DISPLAY_LIMIT = 500

COMMENT_TO_NOTE = "Comment → Note"
DESCRIPTION_TO_DEFINITION = "Description → Definition"
ANNOTATION_TO_EXTENDED_NOTES = "Annotation → Extended Notes"


@dataclass
class DocumentationRow:
    """One object, one mapping, both sides."""
    model: str = ""
    object_type: str = ""          # ENTITY | ATTRIBUTE
    object_name: str = ""          # entity name, or "Entity.Attribute"
    object_code: str = ""
    mapping: str = ""              # one of the COMMENT_/DEFINITION_ constants
    source_field: str = ""         # e.g. "SAP PD Comment"
    target_field: str = ""         # e.g. "erwin Note"
    source_value: str = ""
    target_value: str = ""
    status: str = ""
    similarity: float = 0.0        # 0-100, only meaningful when both populated

    @property
    def is_problem(self) -> bool:
        return self.status in (MISMATCH, MISSING_IN_ERWIN, MISSING_IN_SAP_PD)


# ─── COMPARISON ───────────────────────────────────────────────────────────────

def _classify(source_text: str, target_text: str) -> tuple:
    """Return (status, similarity) for one side-by-side pair."""
    source_text = (source_text or "").strip()
    target_text = (target_text or "").strip()

    if not source_text and not target_text:
        return BOTH_EMPTY, 0.0
    if source_text and not target_text:
        return MISSING_IN_ERWIN, 0.0
    if target_text and not source_text:
        return MISSING_IN_SAP_PD, 0.0

    if normalizers.definitions_match(source_text, target_text):
        return MATCHED, 100.0

    similarity = normalizers.definition_similarity(source_text, target_text)
    return MISMATCH, round(similarity * 100, 2)


def _row(model: str, object_type: str, object_name: str, object_code: str,
         mapping: str, source_field: str, target_field: str,
         source_text: str, target_text: str) -> DocumentationRow:
    status, similarity = _classify(source_text, target_text)
    return DocumentationRow(
        model=model,
        object_type=object_type,
        object_name=object_name,
        object_code=object_code,
        mapping=mapping,
        source_field=source_field,
        target_field=target_field,
        source_value=normalizers.truncate(source_text or "", VALUE_DISPLAY_LIMIT),
        target_value=normalizers.truncate(target_text or "", VALUE_DISPLAY_LIMIT),
        status=status,
        similarity=similarity,
    )


def _pairs_for(pd_object, erwin_object) -> List[tuple]:
    """
    The three mappings for one matched object, as
    (mapping, source_field, target_field, source_text, target_text).

    These are the mappings confirmed at the UI level between the two tools:

        SAP PD Comment      →  erwin Note            (Notes tab)
        SAP PD Description  →  erwin Definition      (Definition tab)
        SAP PD Annotation   →  erwin Extended Notes  (Extended Notes tab)

    The Comment → Note pairing is the one the framework's preprocessing stage
    creates (``app/preprocessing/scripts/pd_comment_to_erwin_note.py``), and
    Notes is where the text is actually visible in the erwin UI, so that is
    what is validated for CDM and LDM.
    """
    return [
        (COMMENT_TO_NOTE, "SAP PD Comment", "erwin Note",
         getattr(pd_object, "doc_comment", "") if pd_object else "",
         getattr(erwin_object, "doc_note", "") if erwin_object else ""),
        (DESCRIPTION_TO_DEFINITION, "SAP PD Description", "erwin Definition",
         getattr(pd_object, "doc_definition", "") if pd_object else "",
         getattr(erwin_object, "doc_definition", "") if erwin_object else ""),
        (ANNOTATION_TO_EXTENDED_NOTES, "SAP PD Annotation",
         "erwin Extended Notes",
         getattr(pd_object, "doc_annotation", "") if pd_object else "",
         getattr(erwin_object, "doc_extended_notes", "") if erwin_object else ""),
    ]


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def build_rows(pd_model, erwin_model,
               entity_pairs: Optional[Dict[str, str]] = None
               ) -> List[DocumentationRow]:
    """
    Produce every documentation row for one validated model pair.

    ``entity_pairs`` optionally maps SAP PD entity key → erwin entity key, as
    resolved by the comparator's matcher.  When omitted, entities are paired on
    normalised name then code — the same basis the comparator's first two
    passes use — so the sheet lines up with the findings.
    """
    model_name = getattr(pd_model, "model_name", "") or ""
    rows: List[DocumentationRow] = []

    pd_entities = _entity_list(pd_model)
    erwin_entities = _entity_list(erwin_model)

    erwin_by_name = {}
    erwin_by_code = {}
    for entity in erwin_entities:
        if entity.name:
            erwin_by_name.setdefault(
                normalizers.normalize_name(entity.name), []).append(entity)
        if entity.code:
            erwin_by_code.setdefault(
                normalizers.normalize_name(entity.code), []).append(entity)

    matched_erwin = set()

    def _take(key):
        """
        First erwin entity under `key` that no SAP PD entity has claimed yet.

        Pairing must be one-to-one. Both models legitimately contain entities
        whose name and code are crossed — e.g. one entity named
        'UNIT_OF_MEASURE' with code 'UNIT OF MEASURES', and a second named
        'Unit Of Measure' with code 'UNIT_OF_MEASURE'. Every one of those
        values normalises to the same key, so a dictionary that returns the
        same entity twice hands the documented twin to both SAP PD entities.
        The undocumented one then reads as MISSING_IN_SAP_PD against text that
        SAP PD does in fact carry — on its sibling.
        """
        if not key:
            return None
        for bucket in (erwin_by_name.get(key), erwin_by_code.get(key)):
            for candidate in bucket or ():
                if id(candidate) not in matched_erwin:
                    return candidate
        return None

    for pd_entity in pd_entities:
        erwin_entity = None
        if entity_pairs and pd_entity.name in entity_pairs:
            erwin_entity = _take(
                normalizers.normalize_name(entity_pairs[pd_entity.name]))
        if erwin_entity is None and pd_entity.name:
            erwin_entity = _take(normalizers.normalize_name(pd_entity.name))
        if erwin_entity is None and pd_entity.code:
            erwin_entity = _take(normalizers.normalize_name(pd_entity.code))

        if erwin_entity is not None:
            matched_erwin.add(id(erwin_entity))

        for mapping, src_field, tgt_field, src, tgt in _pairs_for(pd_entity, erwin_entity):
            rows.append(_row(model_name, "ENTITY", pd_entity.name,
                             pd_entity.code, mapping, src_field, tgt_field,
                             src, tgt))

        # ── Attributes of this entity ────────────────────────────────────────
        if erwin_entity is not None:
            erwin_attrs = {}
            for attribute in getattr(erwin_entity, "attributes", []) or []:
                if attribute.name:
                    erwin_attrs.setdefault(
                        normalizers.normalize_name(attribute.name), attribute)
                if attribute.code:
                    erwin_attrs.setdefault(
                        normalizers.normalize_name(attribute.code), attribute)
        else:
            erwin_attrs = {}

        for pd_attribute in getattr(pd_entity, "attributes", []) or []:
            erwin_attribute = (
                erwin_attrs.get(normalizers.normalize_name(pd_attribute.name or ""))
                or erwin_attrs.get(normalizers.normalize_name(pd_attribute.code or ""))
            )
            label = f"{pd_entity.name}.{pd_attribute.name or pd_attribute.code}"
            for mapping, src_field, tgt_field, src, tgt in _pairs_for(
                    pd_attribute, erwin_attribute):
                rows.append(_row(model_name, "ATTRIBUTE", label,
                                 pd_attribute.code, mapping, src_field,
                                 tgt_field, src, tgt))

    # ── erwin-only entities: documentation added downstream of SAP PD ────────
    for erwin_entity in erwin_entities:
        if id(erwin_entity) in matched_erwin:
            continue
        for mapping, src_field, tgt_field, _src, tgt in _pairs_for(None, erwin_entity):
            if not (tgt or "").strip():
                continue
            rows.append(_row(model_name, "ENTITY", erwin_entity.name,
                             erwin_entity.code, mapping, src_field, tgt_field,
                             "", tgt))

    return rows


def summarise(rows: List[DocumentationRow]) -> Dict[str, Dict[str, int]]:
    """Per-mapping counts of each status, for the sheet's header block."""
    summary: Dict[str, Dict[str, int]] = {}
    for row in rows:
        bucket = summary.setdefault(row.mapping, {
            MATCHED: 0, MISMATCH: 0, MISSING_IN_ERWIN: 0,
            MISSING_IN_SAP_PD: 0, BOTH_EMPTY: 0,
        })
        bucket[row.status] = bucket.get(row.status, 0) + 1
    return summary


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _entity_list(model) -> List:
    """Entities as a list, whether the model stores them as dict or list."""
    entities = getattr(model, "entities", None)
    if entities is None:
        return []
    if isinstance(entities, dict):
        return list(entities.values())
    return list(entities)
