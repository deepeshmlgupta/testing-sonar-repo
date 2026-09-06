"""
UDP Fidelity (SAP PowerDesigner Extended Attributes  →  erwin User-Defined Properties)
=====================================================================================

One common, self-contained module shared by the CDM, LDM and PDM reconciliation
engines.  It answers a single question the structural comparators do not:

    Of the UDP values PowerDesigner actually held, how many does erwin hold today?

It does this in three isolated steps, each usable on its own:

    extract_pd_udps(path)      populated PD Extended Attribute values, per object
    extract_erwin_udps(path)   UDP values present in an erwin XML export, per object
    compare_udps(pd, erwin)    one row per PD value, classified
                                   MATCHED   erwin holds exactly the PD value
                                   MISSING   erwin holds no value for that UDP
                                   MISMATCH  erwin holds a different value

and one convenience wrapper the comparators call at the very end of their
fidelity-calculation stage:

    apply(result, "CDM" | "LDM" | "PDM")

    which attaches UDP counts + UDP Fidelity % to the existing ValidationResult
    and blends UDP fidelity into `result.fidelity_score`.

Design constraints (why the module looks the way it does)
---------------------------------------------------------
* ADDITIVE AND REMOVABLE.  Nothing here is imported by a parser, a matcher, a
  finding emitter or the preprocessing code.  Each comparator has ONE call to
  `apply()` after its own score is computed; each report generator has a handful
  of one-line calls to append UDP columns / a UDP sheet.  Delete this file and
  those lines and the framework is exactly as it was.
* NEVER RAISES.  `apply()` swallows and logs every error.  A broken UDP read must
  not turn a good structural validation into an ERROR.
* REUSES THE UDP TOOL.  Loading / sanitising the PowerDesigner file, the
  definition-vs-pointer filter (`Model._defs`, see UDP_FINDINGS_AND_FIXES.md
  Finding 1) and the `{GUID},Name,len=value` entry grammar all come from
  `app/udp_tool/pd_extract.py`; the erwin UDP naming rule comes from
  `app/udp_tool/erwin_prepare.py`.  A minimal fallback is kept so this module
  still works if the udp_tool folder is ever removed.
* ONE EXTRACTION FOR ALL THREE TIERS.  CDM/LDM objects are Entity /
  EntityAttribute, PDM objects are Table / Column.  Both are handled by the same
  code path — the tier only decides which erwin name (logical Name or
  Physical_Name) is tried first when matching objects.

Populated values
----------------
Only *populated* PD values are scored.  PowerDesigner writes list-type UDPs that
were never set as the literal placeholder ``<unspecified>``; counting those as
"values erwin lost" would invent hundreds of failures out of empty source data
(the UDP tool's BLANK IN SAP category exists for the same reason).  The
placeholder set is configurable: ``UDP_UNPOPULATED_VALUES`` in
app/config/validation_config.py.  Skipped values are still counted and reported
(``udp_blank_in_pd``) so nothing is hidden.

Scoring (Phase 1)
-----------------
    UDP Fidelity %   = MATCHED / (MATCHED + MISSING + MISMATCH) × 100
    Overall Fidelity = structural × (1 − w) + UDP × w       w = UDP_FIDELITY_WEIGHT

MISSING values therefore reduce the overall fidelity score — intentionally, because
UDP migration is not yet complete and the report must say so.  When a model holds
no populated PD UDPs the overall score is left untouched (nothing to migrate).

The structural score is preserved on the result as `structural_fidelity_score`.
The PDM promotion gate reads `fidelity_score_raw`, which is deliberately NOT
blended unless ``UDP_FIDELITY_AFFECTS_PROMOTION_GATE`` is switched on — so a
structurally complete PDM can still be promoted in Phase 1 while its report
shows that UDPs are outstanding.

erwin XML representation of UDP values
--------------------------------------
erwin declares ``xmlns:UDP="http://www.erwin.com/dm/metadata"`` on every export
and writes user-defined property values as children of the owning object's
``<...Props>`` block in that namespace, with the UDP name as the element name
(non-identifier characters encoded as ``_xHHHH_``).  UDP definitions, when
present, live under ``<Udp_Groups>/<Udp>``.  The extractor accepts:

    <EntityProps> ... <UDP:DataConfidentiality>Internal</UDP:DataConfidentiality>
    <EntityProps> ... <UDP:Entity.DataConfidentiality>Internal</UDP:...>
    <EntityProps> ... <UDP:{Long_Id-of-Udp-definition}>Internal</UDP:...>
    <EntityProps> ... <UDP_DataConfidentiality>Internal</UDP_DataConfidentiality>

The exports shipped with the framework carry no UDP values yet (Phase 1), so
every populated PD value is reported MISSING and the UDP_FIDELITY sheet's
diagnostics say why ("0 UDP definitions / 0 UDP values found in erwin XML").
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── statuses ─────────────────────────────────────────────────────────────────
ST_MATCHED = "MATCHED"
ST_MISSING = "MISSING"
ST_MISMATCH = "MISMATCH"
STATUS_ORDER = (ST_MISMATCH, ST_MISSING, ST_MATCHED)

# Report column headers reused across several sheets.
COL_UDP_FIDELITY = "UDP Fidelity %"

# ─── defaults (overridable through the tier config) ───────────────────────────
DEFAULT_ENABLED = True
DEFAULT_WEIGHT = 0.20
DEFAULT_AFFECTS_GATE = False
DEFAULT_UNPOPULATED = ("", "<unspecified>", "<none>", "<undefined>")
DEFAULT_IGNORE_CASE = False
DEFAULT_MAX_DETAIL_ROWS = 5000

# PowerDesigner object types whose UDPs have an erwin counterpart.
_ENTITY_TAGS = {"Entity", "Table"}
_ATTRIBUTE_TAGS = {"EntityAttribute", "Column"}
_MODEL_TAGS = {"Model", "RootObject"}

# erwin
_ERWIN_UDP_NS = "http://www.erwin.com/dm/metadata"
_ERWIN_ENTITY_TAGS = {"Entity", "Table"}
_ERWIN_ATTRIBUTE_TAGS = {"Attribute", "Column"}
_ERWIN_OWNER_PREFIX = re.compile(r"^(Entity|Table|Attribute|Column|Model|Key_Group|Relationship)\.",
                                 re.IGNORECASE)
_ERWIN_UDP_LOCAL = re.compile(r"^(UDP|Udp)(_|\.|\d)", re.IGNORECASE)
_XML_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")

# PowerDesigner ExtendedAttributesText entry:  {GUID},Name,<length>=<value>
# Identical grammar to app/udp_tool/pd_extract.py; the length group is used to
# trim multi-line values exactly.
_ENTRY_RE = re.compile(r"\{[0-9A-F\-]+\},([^,]+),(\d+)=", re.IGNORECASE)

_PD_NS = {"a": "attribute", "c": "collection", "o": "object"}


# ═════════════════════════════════════════════════════════════════════════════
#  Reuse of the existing UDP tool (app/udp_tool)
# ═════════════════════════════════════════════════════════════════════════════

def _load_udp_tool():
    """
    Import the UDP tool's PowerDesigner loader and erwin naming rule.

    Returned as (ModelClass, udp_name_fn); either may be None, in which case the
    minimal fallbacks below are used.  app/udp_tool is a plain script folder
    (no __init__.py) so it is imported as a namespace package — nothing in that
    folder is modified.
    """
    model_cls = None
    udp_name_fn = None
    try:                                                   # pragma: no cover - env dependent
        from app.udp_tool.pd_extract import Model as model_cls  # type: ignore
    except Exception as exc:                               # noqa: BLE001
        logger.debug("udp_tool.pd_extract unavailable, using fallback loader: %s", exc)
    try:                                                   # pragma: no cover - env dependent
        from app.udp_tool.erwin_prepare import udp_name as udp_name_fn  # type: ignore
    except Exception as exc:                               # noqa: BLE001
        logger.debug("udp_tool.erwin_prepare unavailable, using fallback naming: %s", exc)
    return model_cls, udp_name_fn


_PD_MODEL_CLS, _UDP_NAME_FN = _load_udp_tool()

_SAFE = re.compile(r"\W")


def erwin_udp_name(source_path: str) -> str:
    """PD ``BIM-core.DataConfidentiality`` → erwin UDP name ``DataConfidentiality``."""
    if _UDP_NAME_FN is not None:
        try:
            return _UDP_NAME_FN(source_path)
        except Exception as exc:                           # noqa: BLE001
            logger.debug("udp_tool naming unavailable for %s: %s", source_path, exc)
    return _SAFE.sub("_", source_path.split(".")[-1])


class _FallbackPdModel:
    """Minimal stand-in for udp_tool.pd_extract.Model (same public surface)."""

    def __init__(self, path: Path):
        from defusedxml.ElementTree import fromstring as safe_fromstring
        self.path = path
        self.header: Dict[str, str] = {}
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            if line.startswith("<?PowerDesigner"):
                for match in re.finditer(r'([a-zA-Z]+)="([^"]*)"', line):
                    self.header[match.group(1)] = match.group(2)
                break
        content = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", content)
        self.root = safe_fromstring(content)

    def _defs(self, obj_type):
        return [e for e in self.root.findall(f".//o:{obj_type}", namespaces=_PD_NS)
                if e.get("Ref") is None]


def _load_pd_model(path):
    cls = _PD_MODEL_CLS or _FallbackPdModel
    return cls(Path(path))


# ═════════════════════════════════════════════════════════════════════════════
#  Data classes
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class UdpValue:
    """One UDP value on one object, on either side."""
    object_type: str          # ENTITY | ATTRIBUTE | MODEL
    object_name: str
    object_code: str
    owner_name: str = ""      # entity name for attributes
    owner_code: str = ""
    udp_name: str = ""        # erwin-style name (leaf, sanitised)
    source_path: str = ""     # PD  Extension.Property
    value: str = ""


@dataclass
class UdpRow:
    """One comparison outcome, one row on the UDP_DETAIL sheet."""
    object_type: str
    object_name: str
    object_code: str
    owner_name: str
    udp_name: str
    source_path: str
    pd_value: str
    erwin_value: str
    status: str
    note: str = ""


@dataclass
class UdpFidelityResult:
    pd_file: str = ""
    erwin_file: str = ""
    model_type: str = ""
    rows: List[UdpRow] = field(default_factory=list)

    # PD side
    udp_total: int = 0            # populated PD values compared (denominator)
    udp_blank_in_pd: int = 0      # unpopulated / placeholder values skipped
    udp_unscoped: int = 0         # values on PD objects with no erwin counterpart type
    udp_distinct_pd: int = 0      # distinct UDP names carrying a populated value
    udp_objects_pd: int = 0       # PD objects carrying at least one populated value

    # outcome
    udp_matched: int = 0
    udp_missing: int = 0
    udp_mismatch: int = 0

    # erwin side
    udp_definitions_erwin: int = 0
    udp_values_erwin: int = 0
    udp_extra_in_erwin: int = 0   # erwin values with no populated PD counterpart

    notes: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def udp_fidelity_score(self) -> Optional[float]:
        """MATCHED share of populated values, or None when there is nothing to score."""
        if self.udp_total == 0:
            return None
        return round(100.0 * self.udp_matched / self.udp_total, 2)

    def by_udp(self) -> Dict[str, Counter]:
        rollup: Dict[str, Counter] = defaultdict(Counter)
        for row in self.rows:
            rollup[row.udp_name][row.status] += 1
        return rollup

    def summary_line(self) -> str:
        if self.error:
            return f"UDP fidelity not calculated: {self.error}"
        if self.udp_total == 0:
            return "No populated UDP values in SAP PD; UDP fidelity not applicable."
        return (f"UDP fidelity {self.udp_fidelity_score:.2f}%: "
                f"{self.udp_matched}/{self.udp_total} matched, "
                f"{self.udp_missing} missing, {self.udp_mismatch} mismatched.")


# ═════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════════════

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _ns(tag: str) -> str:
    return tag[1:tag.index("}")] if tag.startswith("{") else ""


def _norm(text: str) -> str:
    """Object-name key: case, whitespace and punctuation insensitive."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _norm_value(text: str, ignore_case: bool) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text.lower() if ignore_case else text


def _cfg(config, name: str, default):
    return getattr(config, name, default) if config is not None else default


def _tier_config(model_type: str):
    try:
        from app.config.validation_config import tier
        return tier(model_type)
    except Exception:                                      # noqa: BLE001
        return None


def parse_extended_attributes_text(text: str) -> List[Tuple[str, str, str]]:
    """
    Decode one ``<a:ExtendedAttributesText>`` block.

    Returns [(source_path, property_name, value), ...] where source_path is
    ``Extension.Property`` — the same shape udp_tool's manifest uses.

    Grammar (see udp_tool/pd_extract.py):

        {ext-guid},ExtensionName,<len>={prop-guid},PropName,<len>=<value>
        {prop-guid},PropName,<len>=<value>
        ...

    Values may span several lines (list-valued UDPs do); a line that does not
    start a new entry is a continuation of the previous value.  The declared
    length (which counts CRLF as two characters) trims each value exactly.
    """
    entries: List[List[Any]] = []
    profile = ""
    current: Optional[List[Any]] = None
    for line in (text or "").splitlines():
        matches = list(_ENTRY_RE.finditer(line))
        if matches:
            last = matches[-1]
            if len(matches) > 1:
                profile = matches[-2].group(1).strip()
            current = [profile, last.group(1).strip(), int(last.group(2)), line[last.end():]]
            entries.append(current)
        elif current is not None:
            current[3] += "\n" + line

    out: List[Tuple[str, str, str]] = []
    for profile, name, length, raw in entries:
        crlf = raw.replace("\n", "\r\n")
        if len(crlf) > length:
            raw = crlf[:length].replace("\r\n", "\n")
        value = raw.rstrip("\r\n ").rstrip()
        path = f"{profile}.{name}" if profile else name
        out.append((path, name, value))
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Step 1 — SAP PowerDesigner side
# ═════════════════════════════════════════════════════════════════════════════

def _find_top_level_model(root) -> Optional[Any]:
    for child in root.iter():
        if _local(child.tag) == "RootObject":
            children = child.find("c:Children", namespaces=_PD_NS)
            if children is not None and len(children):
                return children[0]
            return None
    return None


def _classify_pd_owner(owner, tag, text, enclosing, model,
                       top_level_model) -> Optional[Tuple[str, str, str, str, str]]:
    """
    Map an ExtendedAttributesText owner to (obj_type, obj_name, obj_code,
    owner_name, owner_code), or None when the owner is out of scope.
    """
    if tag in _ENTITY_TAGS:
        return "ENTITY", text(owner, "Name"), text(owner, "Code"), "", ""

    if tag in _ATTRIBUTE_TAGS:
        holder = enclosing(owner, _ENTITY_TAGS)
        if holder is None:
            return None
        return ("ATTRIBUTE", text(owner, "Name"), text(owner, "Code"),
                text(holder, "Name"), text(holder, "Code"))

    if owner is top_level_model or tag in _MODEL_TAGS:
        return ("MODEL", text(owner, "Name") or model.header.get("Name", ""),
                text(owner, "Code"), "", "")

    return None


def _collect_pd_values(ext_text: str, meta: Tuple[str, str, str, str, str],
                       unpopulated_keys: set, values: List["UdpValue"],
                       stats: Counter) -> None:
    obj_type, obj_name, obj_code, own_name, own_code = meta
    for source_path, prop, value in parse_extended_attributes_text(ext_text):
        if value.strip().lower() in unpopulated_keys:
            stats["blank"] += 1
            continue
        values.append(UdpValue(object_type=obj_type, object_name=obj_name,
                               object_code=obj_code, owner_name=own_name,
                               owner_code=own_code,
                               udp_name=erwin_udp_name(source_path),
                               source_path=source_path, value=value))


def _process_pd_ext_element(ext, parent_of, text, enclosing, model,
                            top_level_model, unpopulated_keys,
                            values: List["UdpValue"], stats: Counter) -> None:
    owner = parent_of.get(ext)
    if owner is None or owner.get("Ref") is not None or not (ext.text or "").strip():
        return

    meta = _classify_pd_owner(owner, _local(owner.tag), text, enclosing,
                              model, top_level_model)
    if meta is None:
        stats["unscoped"] += len(parse_extended_attributes_text(ext.text))
        return

    _collect_pd_values(ext.text, meta, unpopulated_keys, values, stats)


def extract_pd_udps(path, unpopulated: Iterable[str] = DEFAULT_UNPOPULATED
                    ) -> Tuple[List[UdpValue], Dict[str, int], List[str]]:
    """
    Every Extended Attribute value in a .cdm / .ldm / .pdm, per object.

    Returns (values, stats, notes).  `values` holds populated values on
    entities/tables, attributes/columns and the model root.  `stats` counts what
    was skipped (blank placeholders, values on out-of-scope object types) so the
    report can account for every value the file contained.
    """
    unpopulated_keys = {str(v).strip().lower() for v in unpopulated}
    values: List[UdpValue] = []
    stats = Counter()
    notes: List[str] = []

    model = _load_pd_model(path)
    root = model.root
    parent_of = {child: parent for parent in root.iter() for child in parent}

    def text(elem, name):
        return (elem.findtext(f"a:{name}", default="", namespaces=_PD_NS) or "").strip()

    def enclosing(elem, tags):
        node = parent_of.get(elem)
        while node is not None:
            if _local(node.tag) in tags and node.get("Ref") is None:
                return node
            node = parent_of.get(node)
        return None

    top_level_model = _find_top_level_model(root)

    for ext in root.iter("{attribute}ExtendedAttributesText"):
        _process_pd_ext_element(ext, parent_of, text, enclosing, model,
                                top_level_model, unpopulated_keys, values, stats)

    stats["populated"] = len(values)
    if stats["unscoped"]:
        notes.append(f"{stats['unscoped']} SAP PD extended-attribute value(s) sit on object "
                     f"types with no erwin UDP counterpart (e.g. ExtendedObject) and were "
                     f"not scored.")
    if stats["blank"]:
        notes.append(f"{stats['blank']} SAP PD extended-attribute value(s) are unpopulated "
                     f"placeholders ({', '.join(sorted(v for v in unpopulated_keys if v))}) "
                     f"and were not scored.")
    return values, dict(stats), notes


# ═════════════════════════════════════════════════════════════════════════════
#  Step 2 — erwin side
# ═════════════════════════════════════════════════════════════════════════════

def _decode_erwin_name(local: str) -> str:
    return _XML_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), local)


def _erwin_props_of(elem):
    return next((c for c in elem if _local(c.tag).endswith("Props")), None)


def _erwin_prop_text(props, name: str) -> str:
    if props is None:
        return ""
    for child in props:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _erwin_udp_children(props, definitions: Dict[str, str]) -> List[Tuple[str, str]]:
    found = []
    for child in props:
        local = _local(child.tag)
        if _ns(child.tag) == _ERWIN_UDP_NS:
            raw = _decode_erwin_name(local)
        elif _ERWIN_UDP_LOCAL.match(local):
            raw = _decode_erwin_name(re.sub(r"^(UDP|Udp)[_.]", "", local, flags=re.IGNORECASE))
        else:
            continue
        if len(child):
            # value carried on a nested element (e.g. <Value>)
            text_value = "".join(t.strip() for t in child.itertext())
        else:
            text_value = (child.text or "").strip()
        name = definitions.get(raw) or definitions.get(raw.strip("{}")) or raw
        name = _ERWIN_OWNER_PREFIX.sub("", name)
        found.append((name, text_value))
    return found


def _erwin_definition_display_name(elem, props) -> str:
    name = elem.get("name", "")
    if props is not None:
        for child in props:
            if _local(child.tag) == "Name" and (child.text or "").strip():
                name = child.text.strip()
    return _ERWIN_OWNER_PREFIX.sub("", name)


def _register_erwin_definition(elem, props, name: str,
                               definitions: Dict[str, str]) -> None:
    for key in (elem.get("id", ""), elem.get("name", "")):
        if key:
            definitions[key] = name
    if props is not None:
        for child in props:
            if _local(child.tag) in ("Long_Id", "Udp_Id") and (child.text or "").strip():
                definitions[child.text.strip()] = name


def _collect_erwin_definitions(root, stats: Counter) -> Dict[str, str]:
    """id / Udp_Id / 'Owner.Name' → display name for every <Udp> definition."""
    definitions: Dict[str, str] = {}
    for elem in root.iter():
        if _local(elem.tag) != "Udp":
            continue
        props = _erwin_props_of(elem)
        name = _erwin_definition_display_name(elem, props)
        if not name:
            continue
        stats["definitions"] += 1
        _register_erwin_definition(elem, props, name, definitions)
    return definitions


def _collect_erwin_model_udps(root, definitions: Dict[str, str],
                              values: List["UdpValue"]) -> None:
    for elem in root.iter():
        if _local(elem.tag) != "Model":
            continue
        props = _erwin_props_of(elem)
        if props is not None:
            model_name = _erwin_prop_text(props, "Name") or elem.get("name", "")
            for name, value in _erwin_udp_children(props, definitions):
                values.append(UdpValue("MODEL", model_name, model_name,
                                       udp_name=name, value=value))
        break


def _collect_erwin_attribute_udps(entity, e_name: str, e_phys: str,
                                  definitions: Dict[str, str],
                                  values: List["UdpValue"]) -> None:
    for group in entity:
        if not _local(group.tag).endswith("_Groups"):
            continue
        for attr in group:
            if _local(attr.tag) not in _ERWIN_ATTRIBUTE_TAGS:
                continue
            a_props = _erwin_props_of(attr)
            if a_props is None:
                continue
            a_name = _erwin_prop_text(a_props, "Name") or attr.get("name", "")
            a_phys = _erwin_prop_text(a_props, "Physical_Name")
            for name, value in _erwin_udp_children(a_props, definitions):
                values.append(UdpValue("ATTRIBUTE", a_name, a_phys, e_name, e_phys,
                                       udp_name=name, value=value))


def _collect_erwin_entity_udps(root, definitions: Dict[str, str],
                               values: List["UdpValue"]) -> None:
    for entity in root.iter():
        if _local(entity.tag) not in _ERWIN_ENTITY_TAGS or entity.get("id") is None:
            continue
        e_props = _erwin_props_of(entity)
        if e_props is None:
            continue
        e_name = _erwin_prop_text(e_props, "Name") or entity.get("name", "")
        e_phys = _erwin_prop_text(e_props, "Physical_Name")
        for name, value in _erwin_udp_children(e_props, definitions):
            values.append(UdpValue("ENTITY", e_name, e_phys, udp_name=name, value=value))
        _collect_erwin_attribute_udps(entity, e_name, e_phys, definitions, values)


def extract_erwin_udps(path) -> Tuple[List[UdpValue], Dict[str, int], List[str]]:
    """
    Every UDP value an erwin XML export holds, per object.

    Returns (values, stats, notes).  `stats` reports how many UDP *definitions*
    and *values* the file contains — when both are zero the migration simply has
    not written UDPs yet, and the report says so instead of leaving a reviewer
    to wonder whether the extractor missed them.
    """
    from defusedxml.ElementTree import parse as safe_parse

    values: List[UdpValue] = []
    stats = Counter()
    notes: List[str] = []

    tree = safe_parse(str(path))
    root = tree.getroot()

    definitions = _collect_erwin_definitions(root, stats)
    _collect_erwin_model_udps(root, definitions, values)
    _collect_erwin_entity_udps(root, definitions, values)

    stats["values"] = len(values)
    if not stats["definitions"] and not stats["values"]:
        notes.append("erwin XML contains no UDP definitions and no UDP values: UDP "
                     "migration has not been applied to this export yet.")
    elif not stats["values"]:
        notes.append(f"erwin XML defines {stats['definitions']} UDP(s) but holds no "
                     f"values for them.")
    return values, dict(stats), notes


# ═════════════════════════════════════════════════════════════════════════════
#  Step 3 — comparison and UDP fidelity
# ═════════════════════════════════════════════════════════════════════════════

def _udp_keys(pd: UdpValue) -> List[str]:
    """Every acceptable erwin spelling of a PD property, normalised."""
    leaf = pd.source_path.split(".")[-1]
    return list(dict.fromkeys(k for k in (
        _norm(pd.udp_name), _norm(leaf), _norm(_SAFE.sub("_", pd.source_path)),
        _norm(pd.source_path)) if k))


def _erwin_object_keys(v: UdpValue) -> List[Tuple[str, str]]:
    """Every (object_type, 'owner|self') key an erwin value can be matched on."""
    owner_keys = ({_norm(v.owner_name), _norm(v.owner_code)} - {""}
                  if v.object_type == "ATTRIBUTE" else {""})
    own_keys = {_norm(v.object_name), _norm(v.object_code)} - {""}
    if v.object_type == "MODEL":
        own_keys = {"*model*"}
    return [(v.object_type, f"{o}|{s}") for o in (owner_keys or {""}) for s in own_keys]


def _find_erwin_hit(pd: UdpValue, candidates: List[Tuple[str, str]],
                    erwin_by_object: Dict[Tuple[str, str], Dict[str, Tuple[str, str]]],
                    consumed: set) -> Optional[Tuple[str, str]]:
    """Return the (display_name, value) erwin holds for this PD value, or None."""
    for key in candidates:
        bucket = erwin_by_object.get(key)
        if not bucket:
            continue
        for udp_key in _udp_keys(pd):
            if udp_key in bucket:
                consumed.add((key, udp_key))
                return bucket[udp_key]
    return None


def _classify_udp_row(pd: UdpValue, erwin_hit: Optional[Tuple[str, str]],
                      object_found: bool, ignore_case: bool,
                      result: "UdpFidelityResult") -> Tuple[str, str, str]:
    """Decide MATCHED / MISSING / MISMATCH, update counters, return (status, value, note)."""
    if erwin_hit is None or not erwin_hit[1].strip():
        result.udp_missing += 1
        note = ("erwin holds no UDP values on this object." if not object_found
                else "erwin holds no value for this UDP on this object.")
        return ST_MISSING, "", note

    if _norm_value(erwin_hit[1], ignore_case) == _norm_value(pd.value, ignore_case):
        result.udp_matched += 1
        return ST_MATCHED, erwin_hit[1], ""

    result.udp_mismatch += 1
    return ST_MISMATCH, erwin_hit[1], "erwin holds a different value than SAP PD."


def compare_udps(pd_values: List[UdpValue], erwin_values: List[UdpValue],
                 ignore_case: bool = DEFAULT_IGNORE_CASE) -> UdpFidelityResult:
    """
    Join populated PD values to erwin values, one row per PD value.

    Objects are matched on name OR code/physical name (case, whitespace and
    punctuation insensitive) — the structural comparators already reported any
    rename, so a UDP must not be scored MISSING just because the object it sits on
    was matched through a fallback key.
    """
    result = UdpFidelityResult()

    # erwin lookup: object key -> {udp key -> (display name, value)}
    erwin_by_object: Dict[Tuple[str, str], Dict[str, Tuple[str, str]]] = defaultdict(dict)
    erwin_object_keys = set()

    for v in erwin_values:
        for key in _erwin_object_keys(v):
            erwin_object_keys.add(key)
            erwin_by_object[key][_norm(v.udp_name)] = (v.udp_name, v.value)

    consumed = set()   # (object key, udp key) pairs explained by a PD value

    for pd in pd_values:
        candidates = _erwin_object_keys(pd)
        object_found = any(k in erwin_object_keys for k in candidates)
        erwin_hit = _find_erwin_hit(pd, candidates, erwin_by_object, consumed)

        status, erwin_value, note = _classify_udp_row(
            pd, erwin_hit, object_found, ignore_case, result)

        result.rows.append(UdpRow(
            object_type=pd.object_type, object_name=pd.object_name,
            object_code=pd.object_code, owner_name=pd.owner_name,
            udp_name=pd.udp_name, source_path=pd.source_path,
            pd_value=pd.value, erwin_value=erwin_value, status=status, note=note))

    result.udp_total = len(pd_values)
    result.udp_distinct_pd = len({v.udp_name for v in pd_values})
    result.udp_objects_pd = len({(v.object_type, v.owner_name, v.object_name) for v in pd_values})
    result.udp_values_erwin = len(erwin_values)
    result.udp_extra_in_erwin = sum(
        1 for key, bucket in erwin_by_object.items()
        for udp_key, (_, value) in bucket.items()
        if value.strip() and (key, udp_key) not in consumed) if erwin_values else 0
    return result


def calculate_udp_fidelity(pd_file: str, erwin_file: str, model_type: str = "",
                           config=None) -> UdpFidelityResult:
    """Extract both sides and compare.  Never raises; errors land in `result.error`."""
    config = config if config is not None else _tier_config(model_type or "CDM")
    result = UdpFidelityResult(pd_file=str(pd_file or ""), erwin_file=str(erwin_file or ""),
                               model_type=(model_type or "").upper())
    try:
        if not pd_file or not os.path.exists(pd_file):
            result.error = f"SAP PD file not found: {pd_file}"
            return result
        if not erwin_file or not os.path.exists(erwin_file):
            result.error = f"erwin XML not found: {erwin_file}"
            return result

        pd_values, pd_stats, pd_notes = _cached_pd_extract(
            pd_file, tuple(_cfg(config, "UDP_UNPOPULATED_VALUES", DEFAULT_UNPOPULATED)))
        erwin_values, erwin_stats, erwin_notes = extract_erwin_udps(erwin_file)

        compared = compare_udps(pd_values, erwin_values,
                                ignore_case=bool(_cfg(config, "UDP_COMPARE_IGNORE_CASE",
                                                      DEFAULT_IGNORE_CASE)))
        compared.pd_file, compared.erwin_file, compared.model_type = (
            result.pd_file, result.erwin_file, result.model_type)
        compared.udp_blank_in_pd = pd_stats.get("blank", 0)
        compared.udp_unscoped = pd_stats.get("unscoped", 0)
        compared.udp_definitions_erwin = erwin_stats.get("definitions", 0)
        compared.notes = pd_notes + erwin_notes
        if compared.udp_extra_in_erwin:
            compared.notes.append(f"erwin holds {compared.udp_extra_in_erwin} UDP value(s) "
                                  f"with no populated SAP PD counterpart; not scored.")
        return compared
    except Exception as exc:                               # noqa: BLE001
        logger.warning("UDP fidelity failed for %s vs %s: %s", pd_file, erwin_file, exc)
        result.error = f"{type(exc).__name__}: {exc}"
        return result


# PD extraction is the expensive half and the PDM flow validates the same .pdm
# twice (pass 1 / pass 2), so the PD side is memoised on (path, mtime, size).
_PD_CACHE: Dict[Tuple[str, float, int, tuple], Tuple[List[UdpValue], Dict[str, int], List[str]]] = {}


def _cached_pd_extract(pd_file: str, unpopulated: tuple):
    stat = os.stat(pd_file)
    key = (os.path.abspath(pd_file), stat.st_mtime, stat.st_size, unpopulated)
    if key not in _PD_CACHE:
        if len(_PD_CACHE) > 8:
            _PD_CACHE.clear()
        _PD_CACHE[key] = extract_pd_udps(pd_file, unpopulated)
    return _PD_CACHE[key]


# ═════════════════════════════════════════════════════════════════════════════
#  Integration point for the comparators
# ═════════════════════════════════════════════════════════════════════════════

def blend(structural: float, udp: Optional[float], weight: float) -> float:
    """Overall fidelity = structural × (1 − w) + UDP × w; unchanged when UDP is n/a."""
    if udp is None:
        return structural
    weight = min(max(float(weight), 0.0), 1.0)
    return max(0.0, min(100.0, structural * (1.0 - weight) + udp * weight))


def apply(result, model_type: str, config=None) -> Optional[UdpFidelityResult]:
    """
    Attach UDP fidelity to a comparator's ValidationResult (CDM, LDM or PDM).

    Called once, after the comparator has computed its own score.  Adds:

        result.udp_fidelity              UdpFidelityResult (rows, notes, counts)
        result.udp_total / udp_matched / udp_missing / udp_mismatch
        result.udp_fidelity_score        float | None
        result.structural_fidelity_score the comparator's own score, preserved
        result.fidelity_score            blended overall score

    `fidelity_score_raw` (the PDM promotion gate) is blended only when
    UDP_FIDELITY_AFFECTS_PROMOTION_GATE is True.  ERROR results are left alone.
    Never raises.
    """
    try:
        config = config if config is not None else _tier_config(model_type)
        if not _cfg(config, "UDP_FIDELITY_ENABLED", DEFAULT_ENABLED):
            return None
        if getattr(result, "status", "") == "ERROR":
            return None

        udp = calculate_udp_fidelity(getattr(result, "pd_file", ""),
                                     getattr(result, "erwin_file", ""),
                                     model_type, config)

        structural = float(getattr(result, "fidelity_score", 100.0))
        weight = float(_cfg(config, "UDP_FIDELITY_WEIGHT", DEFAULT_WEIGHT))

        result.udp_fidelity = udp
        result.udp_total = udp.udp_total
        result.udp_matched = udp.udp_matched
        result.udp_missing = udp.udp_missing
        result.udp_mismatch = udp.udp_mismatch
        result.udp_fidelity_score = udp.udp_fidelity_score
        result.udp_fidelity_weight = weight
        result.structural_fidelity_score = structural

        if udp.error or udp.udp_total == 0:
            return udp

        result.fidelity_score = round(blend(structural, udp.udp_fidelity_score, weight), 2)
        if _cfg(config, "UDP_FIDELITY_AFFECTS_PROMOTION_GATE", DEFAULT_AFFECTS_GATE) \
                and hasattr(result, "fidelity_score_raw"):
            result.fidelity_score_raw = blend(float(result.fidelity_score_raw),
                                              udp.udp_fidelity_score, weight)
        threshold = _cfg(config, "FIDELITY_REVIEW_THRESHOLD", None)
        if threshold is not None:
            result.needs_review = bool(getattr(result, "needs_review", False)) or \
                result.fidelity_score < float(threshold)
        logger.info("%s %s: %s overall fidelity %.2f%% (structural %.2f%%, weight %.2f)",
                    model_type, os.path.basename(str(getattr(result, "pd_file", ""))),
                    udp.summary_line(), result.fidelity_score, structural, weight)
        return udp
    except Exception as exc:                               # noqa: BLE001
        logger.warning("UDP fidelity skipped for %s: %s",
                       getattr(result, "pd_file", "?"), exc)
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  Report helpers — shared by the CDM, LDM and PDM Excel generators
# ═════════════════════════════════════════════════════════════════════════════

SUMMARY_HEADERS = [
    "UDPs (SAP PD)", "UDPs Matched", "UDPs Missing", "UDPs Mismatch",
    COL_UDP_FIDELITY, "Structural Fidelity %",
]
SUMMARY_WIDTHS = [13, 13, 13, 13, 13, 17]
_SUMMARY_TOTAL_ATTRS = ("udp_total", "udp_matched", "udp_missing", "udp_mismatch")


def summary_values(result) -> list:
    """The SUMMARY-row cells for `SUMMARY_HEADERS`, in order."""
    score = getattr(result, "udp_fidelity_score", None)
    return [
        getattr(result, "udp_total", 0), getattr(result, "udp_matched", 0),
        getattr(result, "udp_missing", 0), getattr(result, "udp_mismatch", 0),
        score if score is not None else "n/a",
        getattr(result, "structural_fidelity_score", getattr(result, "fidelity_score", "")),
    ]


def summary_totals(ws, total_row: int, first_column: int, results, style=None) -> None:
    """TOTAL-row cells for the UDP columns (counts summed, percentages averaged)."""
    for offset, attribute in enumerate(_SUMMARY_TOTAL_ATTRS):
        cell = ws.cell(total_row, first_column + offset,
                       sum(int(getattr(r, attribute, 0) or 0) for r in results))
        if style:
            style(cell)
    total = sum(int(getattr(r, "udp_total", 0) or 0) for r in results)
    matched = sum(int(getattr(r, "udp_matched", 0) or 0) for r in results)
    cell = ws.cell(total_row, first_column + 4,
                   round(100.0 * matched / total, 2) if total else "n/a")
    if style:
        style(cell)
    if results:
        cell = ws.cell(total_row, first_column + 5, round(sum(
            float(getattr(r, "structural_fidelity_score", getattr(r, "fidelity_score", 0.0)))
            for r in results) / len(results), 2))
        if style:
            style(cell)


def dashboard_statistics(results) -> List[Tuple[str, Any]]:
    """Extra (label, value) rows for the DASHBOARD 'Run Statistic' block."""
    total = sum(int(getattr(r, "udp_total", 0) or 0) for r in results)
    matched = sum(int(getattr(r, "udp_matched", 0) or 0) for r in results)
    return [
        ("UDP values compared (SAP PD)", total),
        ("UDP values matched in erwin", matched),
        ("UDP values missing in erwin", sum(int(getattr(r, "udp_missing", 0) or 0) for r in results)),
        ("UDP values mismatched", sum(int(getattr(r, "udp_mismatch", 0) or 0) for r in results)),
        ("UDP fidelity %", round(100.0 * matched / total, 2) if total else "n/a"),
    ]


_SHEET_SUMMARY = "UDP_FIDELITY"
_SHEET_DETAIL = "UDP_DETAIL"


def _udp_summary_row_values(result, clean) -> list:
    udp = getattr(result, "udp_fidelity", None)
    score = getattr(result, "udp_fidelity_score", None)
    notes = "; ".join(udp.notes) if udp else "UDP fidelity not calculated."
    if udp and udp.error:
        notes = f"ERROR: {udp.error}. {notes}"
    return [
        os.path.basename(str(getattr(result, "pd_file", ""))),
        getattr(udp, "model_type", "") if udp else "",
        getattr(result, "udp_total", 0), getattr(result, "udp_matched", 0),
        getattr(result, "udp_missing", 0), getattr(result, "udp_mismatch", 0),
        score if score is not None else "n/a",
        getattr(result, "structural_fidelity_score", getattr(result, "fidelity_score", "")),
        getattr(result, "udp_fidelity_weight", ""),
        getattr(result, "fidelity_score", ""),
        getattr(udp, "udp_blank_in_pd", 0) if udp else 0,
        getattr(udp, "udp_definitions_erwin", 0) if udp else 0,
        getattr(udp, "udp_values_erwin", 0) if udp else 0,
        clean(notes),
    ]


def _write_udp_summary_section(ws, results, style, Font) -> int:
    """Write the per-model summary table; returns the next free row."""
    style["header"](ws, ["Model", "Model Type", "UDP values (SAP PD, populated)",
                         "Matched", "Missing", "Mismatch", COL_UDP_FIDELITY,
                         "Structural Fidelity %", "Weight", "Overall Fidelity %",
                         "Unpopulated (skipped)", "erwin UDP definitions",
                         "erwin UDP values", "Notes"], 4)
    row = 5
    for result in results:
        score = getattr(result, "udp_fidelity_score", None)
        values = _udp_summary_row_values(result, style["clean"])
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row, col, value)
            cell.border = style["border"]
            cell.alignment = style["wrap"] if col == 14 else style["center"]
        if isinstance(score, (int, float)) and score < 100:
            ws.cell(row, 7).font = Font(bold=True, color="FFC00000")
        row += 1
    return row


def _write_udp_rollup_result(ws, result, style, row: int) -> int:
    udp = getattr(result, "udp_fidelity", None)
    if not udp:
        return row
    paths = defaultdict(set)
    for r in udp.rows:
        paths[r.udp_name].add(r.source_path)
    for name, counts in sorted(udp.by_udp().items()):
        total = sum(counts.values())
        values = [os.path.basename(str(getattr(result, "pd_file", ""))), name,
                  ", ".join(sorted(paths[name])), total, counts[ST_MATCHED],
                  counts[ST_MISSING], counts[ST_MISMATCH],
                  round(100.0 * counts[ST_MATCHED] / total, 2) if total else "n/a"]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row, col, value)
            cell.border = style["border"]
            if col >= 4:
                cell.alignment = style["center"]
        row += 1
    return row


def _write_udp_rollup_section(ws, results, style, row: int, section_font) -> None:
    row += 1
    ws.cell(row, 1, "Per-UDP rollup").font = section_font
    row += 1
    style["header"](ws, ["Model", "UDP Name", "Source Path (SAP PD)", "Values",
                         "Matched", "Missing", "Mismatch", COL_UDP_FIDELITY], row)
    row += 1
    for result in results:
        row = _write_udp_rollup_result(ws, result, style, row)


def _detail_row_owner(r) -> str:
    if r.object_type == "ATTRIBUTE":
        return r.owner_name
    return r.object_name if r.object_type == "ENTITY" else "(model)"


def _write_udp_detail_result(ws, result, style, row: int, max_rows: int, Font) -> int:
    """Write one result's detail rows; returns the next free row."""
    udp = getattr(result, "udp_fidelity", None)
    if not udp:
        return row

    order = {status: index for index, status in enumerate(STATUS_ORDER)}
    rows = sorted(udp.rows, key=lambda r: (order.get(r.status, 9), r.owner_name.lower(),
                                           r.object_name.lower(), r.udp_name))
    if max_rows and len(rows) > max_rows:
        rows = rows[:max_rows]

    model = os.path.basename(str(getattr(result, "pd_file", "")))
    clean = style["clean"]
    for r in rows:
        values = [model, r.object_type, _detail_row_owner(r), r.object_name,
                  r.object_code, r.udp_name, r.source_path, clean(r.pd_value),
                  clean(r.erwin_value), r.status, clean(r.note)]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row, col, value)
            cell.border = style["border"]
            if col in (8, 9, 11):
                cell.alignment = style["wrap"]
        fill = style["status_fill"].get(r.status)
        if fill:
            ws.cell(row, 10).fill = fill
        row += 1

    if max_rows and len(udp.rows) > max_rows:
        ws.cell(row, 1, f"{model}: {len(udp.rows) - max_rows} more row(s) omitted "
                        f"(UDP_MAX_DETAIL_ROWS = {max_rows}).").font = Font(italic=True)
        row += 1
    return row


def build_sheets(wb, results, config=None, tier_label: str = "") -> None:
    """
    Add the UDP_FIDELITY (per-model summary + per-UDP rollup) and UDP_DETAIL
    (one row per populated PD value) sheets to an existing workbook.

    Self-contained styling so no report generator has to export its helpers.
    """
    try:
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:                                    # pragma: no cover
        return

    header_fill = PatternFill("solid", fgColor="FF1F4E79")
    header_font = Font(bold=True, color="FFFFFFFF")
    section_font = Font(bold=True, size=11)
    thin = Side(style="thin", color="FFBFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    wrap = Alignment(vertical="top", wrap_text=True)
    status_fill = {
        ST_MATCHED: PatternFill("solid", fgColor="FFC6EFCE"),
        ST_MISSING: PatternFill("solid", fgColor="FFFFC7CE"),
        ST_MISMATCH: PatternFill("solid", fgColor="FFFFEB9C"),
    }

    def header(ws, values, row):
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row, col, value)
            cell.fill, cell.font, cell.border, cell.alignment = header_fill, header_font, border, center
        ws.row_dimensions[row].height = 30

    def widths(ws, values):
        for idx, width in enumerate(values, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = width

    def clean(value):
        if value is None:
            return ""
        text = str(value)
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        return text if len(text) <= 2000 else text[:2000] + " ...[truncated]"

    style = {"header": header, "widths": widths, "clean": clean, "border": border,
             "center": center, "wrap": wrap, "status_fill": status_fill}

    max_rows = int(_cfg(config, "UDP_MAX_DETAIL_ROWS", DEFAULT_MAX_DETAIL_ROWS) or 0)

    # ── UDP_FIDELITY ─────────────────────────────────────────────────────────
    ws = wb.create_sheet(_SHEET_SUMMARY)
    ws["A1"] = (f"UDP Fidelity — SAP PD Extended Attributes vs erwin User-Defined Properties"
                f"{'  |  ' + tier_label if tier_label else ''}")
    ws["A1"].font = Font(bold=True, size=13)
    ws.merge_cells("A1:N1")
    ws["A2"] = ("Only populated SAP PD values are scored. UDP Fidelity % = MATCHED / "
                "(MATCHED + MISSING + MISMATCH). Overall Fidelity % = Structural × (1 − weight) "
                "+ UDP × weight. Phase 1: MISSING values lower the overall score because UDP "
                "migration is not complete.")
    ws["A2"].alignment = wrap
    ws.merge_cells("A2:N2")
    ws.row_dimensions[2].height = 34

    row = _write_udp_summary_section(ws, results, style, Font)
    _write_udp_rollup_section(ws, results, style, row, section_font)
    widths(ws, [34, 11, 16, 10, 10, 10, 13, 13, 8, 13, 13, 13, 13, 70])
    ws.freeze_panes = "A5"

    # ── UDP_DETAIL ───────────────────────────────────────────────────────────
    ws = wb.create_sheet(_SHEET_DETAIL)
    header(ws, ["Model", "Object Type", "Entity / Table", "Object", "Code", "UDP Name",
                "Source Path (SAP PD)", "SAP PD Value", "erwin Value", "Status", "Note"], 1)
    ws.freeze_panes = "A2"
    row = 2
    for result in results:
        row = _write_udp_detail_result(ws, result, style, row, max_rows, Font)
    widths(ws, [30, 11, 30, 30, 22, 30, 44, 40, 40, 11, 50])
    if row > 2:
        ws.auto_filter.ref = f"A1:K{row - 1}"


__all__ = [
    "ST_MATCHED", "ST_MISSING", "ST_MISMATCH",
    "UdpValue", "UdpRow", "UdpFidelityResult",
    "parse_extended_attributes_text", "extract_pd_udps", "extract_erwin_udps",
    "compare_udps", "calculate_udp_fidelity", "blend", "apply",
    "SUMMARY_HEADERS", "SUMMARY_WIDTHS", "summary_values", "summary_totals",
    "dashboard_statistics", "build_sheets",
]
