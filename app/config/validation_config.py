class _Shared:

    # ─── PATHS ────────────────────────────────────────────────────────────────
    # app/main.py supplies all real paths. Blank = "not configured standalone".
    PD_MODELS_DIR = ""
    ERWIN_MODELS_DIR = ""
    OUTPUT_DIR = ""

    ERWIN_EXTENSIONS = [".xml", ".erwin"]

    # ─── MODEL MATCHING ───────────────────────────────────────────────────────
    # How a PowerDesigner file is paired with its erwin export:
    #   "filename"  → strip extension, match on same base name (Sales.ldm ↔ Sales.xml)
    #   "prefix"    → first PREFIX_LENGTH chars of base name
    #   "csv"       → use MAPPING_CSV to specify pairs explicitly
    MATCH_STRATEGY = "filename"

    MAPPING_CSV = ""

    CASE_INSENSITIVE = True

    CHECK_DATA_TYPES = True

    FIDELITY_WEIGHTS = {
        "CRITICAL": 1.0,
        "WARNING": 0.35,
        "INFO": 0.05,
    }
    FIDELITY_MAX_PENALTY_PER_OBJECT = 1.0

    # ─── PERFORMANCE ──────────────────────────────────────────────────────────
    MAX_WORKERS = 8

    # ─── REPORT SETTINGS ──────────────────────────────────────────────────────
    # Limit detail rows per model in Excel (0 = unlimited)
    MAX_DIFF_ROWS_PER_MODEL = 500


    # ─── UDP FIDELITY (app/validation/udp_fidelity.py) ────────────────────────
    # Scores SAP PD Extended Attributes against the UDPs the erwin XML export
    # carries, and blends that into the fidelity score. Setting
    # UDP_FIDELITY_ENABLED = False removes the layer's effect entirely.
    UDP_FIDELITY_ENABLED = True
    UDP_FIDELITY_WEIGHT = 0.20            # overall = structural x 0.8 + UDP x 0.2
    UDP_FIDELITY_AFFECTS_PROMOTION_GATE = False
    UDP_UNPOPULATED_VALUES = ("", "<unspecified>", "<none>", "<undefined>")
    UDP_COMPARE_IGNORE_CASE = False
    UDP_MAX_DETAIL_ROWS = 5000            # per model, on the UDP_DETAIL sheet

    # ─── UDP ENGINE / PHASE D (app/validation/udp_flow.py) ────────────────────
    # Runs the standalone UDP tool (app/udp_tool) as a phase of app/main.py:
    #   1a classify + baseline   1b schema + manifest
    #   2  inject into .erwin    3 mapping workbook    4 read-back comparison
    # UDP_TOOL_ENABLED = False skips the phase entirely.
    UDP_TOOL_ENABLED = True
    # Phase 2 needs Windows + erwin Data Modeler + pywin32. When they are not
    # present the phase records why and continues to phases 3 and 4, which read
    # whatever is on disk.
    UDP_TOOL_INJECT_ENABLED = True
    # "bare"      erwin UDP name = PD_ObjectID          (matches udp_fidelity)
    # "qualified" erwin UDP name = Entity.Logical.PD_ObjectID  (tool default)
    # Bare naming is used here because udp_fidelity.py strips a single owner
    # prefix, so a qualified name never matches its PowerDesigner counterpart.
    UDP_TOOL_NAME_STYLE = "bare"
    UDP_TOOL_READBACK_METHOD = "auto"     # auto | com | binary
    # Per-model working directory. The standalone tool shares one baseline
    # folder across a batch, which is safe only because it runs strictly
    # sequentially; inside the pipeline loop each model gets its own.
    UDP_TOOL_WORKDIR = "data/udp"
    UDP_TOOL_EXTRACTION_ID = "46603045"   # PowerDesigner repository extraction id
    UDP_TOOL_TIMEOUT_SECONDS = 1800
    UDP_TOOL_REQUIRE_ERWIN_BINARY = False
    # Where the source .erwin binary is looked for, in order.
    UDP_TOOL_ERWIN_INPUT_DIRS = (
        "erwinmodels/1_initial/erwin",
        "app/udp_tool/input_erwin_models",
    )
    # Where the binary to READ BACK is looked for, in order: this run's injected
    # output first, then any previously injected model, then the raw input.
    UDP_TOOL_ERWIN_READBACK_DIRS = (
        "erwinmodels/2_preprocessed/erwin",
        "app/udp_tool/output_erwin_models",
        "erwinmodels/1_initial/erwin",
        "app/udp_tool/input_erwin_models",
    )
    # The UDP tool's own raw workbooks stay in its existing report folder.
    UDP_TOOL_REPORT_DIR = "app/udp_tool/output_excel_reports"

    # ─── STAGED FIDELITY V1 -> V2 -> V3 (app/validation/fidelity_stages.py) ───
    # Every stage is measured against a DIFFERENT erwin artefact and every
    # component recomputed; nothing is carried forward.
    #   V1  vs erwinmodels/1_initial/xml       raw export: no comments, no UDPs
    #   V2  vs erwinmodels/2_preprocessed/xml  after Comments/Notes or repairs
    #   V3  vs the enriched .erwin read-back   after the UDP engine (Phase D)
    # Components with nothing to measure for a given model are dropped and the
    # remaining weights renormalised, so a model is never penalised for
    # metadata it never had.
    FIDELITY_STAGE_WEIGHTS = {
        "structural": 0.50,
        "documentation": 0.25,
        "udp": 0.25,
    }
    # Shortcuts and Tags are NOT scored: erwin's XML export has no shortcut
    # object, so a shortcut reads as missing at every stage alike (a constant
    # drag, not a progression), and erwin Tags have no representation anywhere
    # in the framework. Both are reported as "not measured" rather than zero.

    # ─── REPORTS (app/reporting/) ─────────────────────────────────────────────
    # Each model gets its own folder inside its model type's reporting folder,
    # holding its V1 initial, V2 UDP mapping and V3 final reports.
    V3_REPORT_ENABLED = True
    # Tier sheets the consolidated V3 report must never carry. UDP_DETAIL is
    # the full per-value dump.
    V3_REPORT_EXCLUDE_SHEETS = ("UDP_DETAIL",)

# ══════════════════════════════════════════════════════════════════════════════
#  _Semantic — the 41 settings CDM and LDM share and PDM has no use for.
#
#  Both tiers reconcile a *business vocabulary*: entities, attributes,
#  identifiers, relationships, cardinality, inheritance, domains, business rules.
#  A physical model has tables and columns instead, so none of this applies to
#  PDM and none of it is inherited by it.
#
#  Layers protected:
#      STRUCTURE   entities, attributes, identifiers
#      SEMANTICS   relationships, cardinality, optionality, inheritance
#      VOCABULARY  business names, definitions, domains
#      GOVERNANCE  business rules, subject areas, model-quality rules
# ══════════════════════════════════════════════════════════════════════════════
class _Semantic(_Shared):

    # ─── MODEL MATCHING ───────────────────────────────────────────────────────
    PREFIX_LENGTH = 8

    # ─── OBJECT MATCHING ──────────────────────────────────────────────────────
    # These models are keyed on *business* vocabulary, and the two tools do not
    # agree on where that vocabulary lives:
    #
    #   PowerDesigner   Name = "Customer Master (KNA1)"   Code = "KNA1"
    #   erwin logical   Name = "Customer Master (KNA1)"   Physical_Name = "KNA1"
    #
    #   "code"        → match on Code / Physical_Name only
    #   "name"        → match on business Name only
    #   "normalized"  → strip case, spaces, underscores, hyphens, plural "s"
    #   "auto"        → try code, then name, then normalized (recommended)
    ENTITY_MATCH_KEY = "auto"
    ATTRIBUTE_MATCH_KEY = "auto"

    # When an object matches only on a fallback key (e.g. normalized name rather
    # than code), raise a finding so the rename is visible instead of silent.
    REPORT_FALLBACK_MATCHES = True

    # Strip a trailing plural "s" during normalized matching (CUSTOMER ↔ CUSTOMERS).
    NORMALIZE_PLURALS = True

    # ─── VALIDATION TOGGLES : STRUCTURE ───────────────────────────────────────
    CHECK_ENTITIES = True
    CHECK_ATTRIBUTES = True
    CHECK_PRIMARY_IDENTIFIERS = True
    CHECK_ALTERNATE_IDENTIFIERS = True
    CHECK_ATTRIBUTE_ORDER = False        # Attribute order is rarely governed

    # ─── VALIDATION TOGGLES : SEMANTICS ───────────────────────────────────────
    CHECK_RELATIONSHIPS = True
    CHECK_CARDINALITY = True
    CHECK_OPTIONALITY = True             # mandatory / optional at each relationship end
    CHECK_DEPENDENCY = True              # identifying (dependent) vs non-identifying
    CHECK_ROLE_NAMES = True              # verb phrases in both directions
    CHECK_INHERITANCE = True             # generalisation / supertype-subtype trees
    CHECK_ASSOCIATIONS = True            # many-to-many associations / associative entities
    CHECK_MANDATORY_ATTRS = True         # attribute-level mandatory flag

    # ─── VALIDATION TOGGLES : VOCABULARY ──────────────────────────────────────
    CHECK_BUSINESS_NAMES = True          # Name differs while Code matches (or vice-versa)
    CHECK_DEFINITIONS = True             # Comment / Description / Definition text
    CHECK_DOMAINS = True                 # domain (reusable semantic type) assignment

    # Definitions are prose; require this much similarity before flagging a change.
    # 1.0 = must be identical, 0.0 = never flag. 0.90 tolerates whitespace/casing.
    DEFINITION_SIMILARITY_THRESHOLD = 0.90

    # Flag attributes/entities that have a definition on one side and none on the
    # other. Definition loss is the most common silent migration defect.
    FLAG_DEFINITION_LOSS = True

    # ─── VALIDATION TOGGLES : GOVERNANCE ──────────────────────────────────────
    CHECK_BUSINESS_RULES = True
    CHECK_SUBJECT_AREAS = True           # package / subject-area membership
    CHECK_MODEL_QUALITY = True           # orphan entities, entities without identifiers

# ERwin XML stores relationship and entity enum values as numeric codes instead of readable text.
# These mappings convert the codes into meaningful values; unknown codes raise a warning instead of being guessed.
# For relationship cardinality, negative codes represent predefined options, while positive values mean "Exactly n".
    ERWIN_CARDINALITY_CODES = {
        "-3": "0,n",   # Zero, One or More  (0..*)  — erwin's default
        "-2": "0,n",   # Zero, One or More  (0..*)  — confirmed via the erwin UI
                       # on Relationship_1, the only -2 observed; NOT "One or
                       # More (P)", which was an unverified guess.
        "-1": "0,1",   # Zero or One        (0..1)  (Z)
    }

    # Relationship <Null_Option_Type>: does the child end permit nulls?
    ERWIN_NULL_OPTION_CODES = {
        "101": False,  # nulls NOT allowed -> child participation mandatory
        "100": True,   # nulls allowed     -> child participation optional
    }

    # Relationship <Type>. 4 and 9 are established empirically (every Type=4
    # matched a PowerDesigner M:N; every Type=9 lacks verb phrases and has no
    # PowerDesigner counterpart, which is how erwin encodes a subtype). 2 vs 7
    # are both ordinary parent-child relationships; identifying-ness no longer
    # affects cardinality because Null_Option_Type supplies it directly.
    ERWIN_RELATIONSHIP_TYPE_CODES = {
        "2": "IDENTIFYING",
        "7": "NON_IDENTIFYING",
        "4": "MANY_TO_MANY",
        "9": "SUBTYPE",
    }

    # Treat erwin Type=9 relationships as inheritance rather than as
    # relationships. Leaving them in the relationship list reports each one as
    # EXTRA_IN_ERWIN.
    ERWIN_SUBTYPE_AS_INHERITANCE = True

    # If every relationship in the model resolves to the same cardinality, the
    # validator suspects erwin's values never reached the parser. Real models
    # vary; a constant is the signature of a parse failure.
    ERWIN_FLAG_COLLAPSED_CARDINALITY = True

    # ─── TYPE COMPARISON ──────────────────────────────────────────────────────
    # erwin logical models frequently carry only four coarse logical types
    # (Text / Number / Datetime / Blob) while PowerDesigner keeps a richer set
    # (VA, LA, I, LI, DC, F, MN, D, DT, TS …). Comparing those strictly produces
    # thousands of false positives, so choose the strictness you want:
    #
    #   "exact"      → raw strings must match after trimming
    #   "canonical"  → map both sides to a canonical type
    #                  (VA→VARCHAR, LA→LONG_CHAR, I→INTEGER, DC→DECIMAL …)
    #   "family"     → map both sides to a broad family
    #                  (TEXT | NUMBER | TEMPORAL | BOOLEAN | BINARY | OTHER)
    #
    # "family" is the recommended default: it avoids false mismatches when the
    # two tools use different names for essentially the same type. LDM narrows
    # this further with PD_TO_ERWIN_APPROVED_TYPES.
    TYPE_COMPARISON_MODE = "family"

    # Compare declared length / precision when both sides supply them. Where
    # either side is silent the check is skipped rather than failed.
    CHECK_LENGTH_PRECISION = True

# Controls the severity of finding categories without changing the validation code.
# Only exceptions are listed here; anything not listed uses the default severity.
# Valid values are CRITICAL, WARNING, INFO, or IGNORE.
# This allows known migration differences to be downgraded instead of disabling the check.
# LDM overrides specific findings such as IDENTIFIER_UNDERSPECIFIED and IDENTIFYING_KEY_MIGRATION to INFO.
# PRIMARY_IDENTIFIER remains CRITICAL so genuine key/identity loss is still reported as a failure.

    SEVERITY_OVERRIDES = {}

    # Models scoring below this are flagged for mandatory manual review.
    # PDM deliberately uses a lower bar; see PDM.FIDELITY_REVIEW_THRESHOLD.
    FIDELITY_REVIEW_THRESHOLD = 95.0

    # ─── EXCLUSIONS ───────────────────────────────────────────────────────────
    # Entities / attributes whose name or code matches any of these regexes are
    # ignored on both sides (staging scaffolding, tool-generated artefacts, …).
    EXCLUDE_ENTITY_PATTERNS = [
        # r"^TMP_",
        # r"_BAK$",
    ]
    EXCLUDE_ATTRIBUTE_PATTERNS = [
        # r"^ZZ_",
    ]

    # ─── PERFORMANCE ──────────────────────────────────────────────────────────
    # Save a resume checkpoint every N completed pairs
    CHECKPOINT_EVERY = 100

    # ─── REPORT SETTINGS ──────────────────────────────────────────────────────
    # Emit per-model detail sheets only when the run is this size or smaller.
    # Above this, the FINDINGS sheet remains complete and authoritative.
    MAX_MODELS_FOR_DETAIL_SHEETS = 200

    # Additional machine-readable exports alongside the workbook
    EXPORT_FINDINGS_CSV = False
    EXPORT_JSON_SUMMARY = False

    # Excel body font. Report headers always use bold white on navy.
    REPORT_FONT = "Calibri"


# ══════════════════════════════════════════════════════════════════════════════
#  CDM — Conceptual Data Model
#  Inherits _Semantic. Only what is CDM-specific appears below.
#  A CDM carries semantics, not storage: no foreign keys, no physical types.
# ══════════════════════════════════════════════════════════════════════════════
class CDM(_Semantic):

    PD_EXTENSIONS = [".cdm", ".xml"]
    REPORT_FILENAME = "cdm_validation_report.xlsx"

    # A conceptual model is a business vocabulary; shortcuts to objects owned
    # by other models are a workspace-organisation concern, not a conceptual
    # one, so the CDM report neither counts nor lists them.
    CHECK_SHORTCUTS = False

    # ─── ERWIN KEY MIGRATION ──────────────────────────────────────────────────
# ERWIN may automatically migrate parent key attributes to child entities, while PowerDesigner CDM does not.
# This setting controls whether those migrated attributes are ignored, logged as INFO, or treated as extra attributes; 
# LDM uses "relationship" because it handles identifying relationship key migration.

    ERWIN_MIGRATED_KEY_HANDLING = "ignore"

#   # ERWIN may automatically add parent key members to a child's PK, while PowerDesigner only does this for Dependent relationships.
# This setting controls whether such migrated keys are treated as expected migration differences or reported as PRIMARY_IDENTIFIER issues.

    RECOGNISE_ERWIN_KEY_MIGRATION = True

# ERWIN may add parent key members to a child's PK, while PowerDesigner does this only for Dependent relationships.
# This setting decides whether these migrated keys are treated as expected differences or as PRIMARY_IDENTIFIER issues.
    REPORT_ERWIN_INVERSION_ENTRIES = True


# ══════════════════════════════════════════════════════════════════════════════
#  LDM — Logical Data Model
#  Inherits _Semantic. Only what is LDM-specific appears below.
#  An LDM adds the beginnings of storage detail: identifiers are expected to be
#  fully populated, relationships carry dependency, data types are governed.
# ══════════════════════════════════════════════════════════════════════════════
class LDM(_Semantic):

    PD_EXTENSIONS = [".ldm", ".xml"]
    REPORT_FILENAME = "ldm_validation_report.xlsx"

    # Logical models routinely reference shared models (glossary categories,
    # entities of a core model), so PD's "List of Shortcuts" is reported —
    # one FINDINGS row per shortcut, and nothing at all when there are none.
    CHECK_SHORTCUTS = True

# # ERwin migrates parent keys into child entities for identifying relationships, which PD LDM also supports.
# Therefore, only keys migrated through non-identifying relationships are treated as differences.
# "ignore"/"relationship"/"info" handle these as expected migration behavior; "strict" reports them as extra attributes.
# CDM uses "ignore" because CDM does not migrate keys.

    ERWIN_MIGRATED_KEY_HANDLING = "relationship"

# # ERWIN attribute null-option codes use a separate code set from relationship-level null-option codes.
# Keep this separate from PDM's setting because it answers the opposite question: "Is this attribute nullable?"
    ERWIN_ATTRIBUTE_NULL_OPTION_CODES = {
        "1": False,   # not null      -> mandatory
        "0": True,    # nulls allowed -> optional
    }

    # Which erwin key groups to skip during comparison because erwin generates
    # them automatically and they have no SAP PD LDM counterpart. When erwin
    # creates a migrated foreign key from a relationship it can also create an
    # IF key group.
    ERWIN_IGNORED_KEY_GROUP_TYPES = ("IE", "INVERSION ENTRY", "INDEX",
                                     "IF", "IF1", "IF2", "IF3", "IF4", "IF5")

# # Defines the approved PowerDesigner → ERwin data type mappings used before the general comparison rules.
# Only listed equivalents are accepted; other types must match exactly or are reported as DATA_TYPE mismatches.

    PD_TO_ERWIN_APPROVED_TYPES = {
        "CHAR": {"CHAR"},
        "VARCHAR": {"VARCHAR"},
        "TEXT": {"LONG_TEXT", "CLOB", "TEXT", "LONGTEXT"},
        "INTEGER": {"INTEGER"},
        "SMALLINT": {"SMALLINT"},
        "BIGINT": {"BIGINT"},
        "DECIMAL": {"DECIMAL"},
        "DATE": {"DATE"},
        "TIME": {"TIME"},
        "TIMESTAMP": {"TIMESTAMP"},
        "BINARY": {"BINARY"},
        "VARBINARY": {"VARBINARY"},
        "BOOLEAN": {"BOOLEAN", "BIT", "BOOL"},
        "XML": {"XML"},
    }

    # Business requirement: a length/precision mismatch on an otherwise
    # type-matching attribute (e.g. DECIMAL(10,2) vs DECIMAL(12,4)) must be
    # visible as a WARNING, not the framework's INFO default. This is the only
    # tier that overrides the empty _Semantic.SEVERITY_OVERRIDES.
    SEVERITY_OVERRIDES = {
        "LENGTH_PRECISION": "WARNING",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  PDM — Physical Data Model
#  Inherits _Shared only: none of the _Semantic entity/relationship vocabulary
#  applies to tables and columns.
#
#  This tier's engine reads its settings with getattr() defaults and uses an
#  EXHAUSTIVE severity table rather than sparse overrides.
# ══════════════════════════════════════════════════════════════════════════════
class PDM(_Shared):

    PD_EXTENSIONS = [".pdm"]

    # The PDM bridge REASSIGNS this at runtime so the pipeline can name the
    # workbook to match its other reports. It must stay a plain writable
    # attribute.
    REPORT_FILENAME = "validation_report.xlsx"

    # ─── VALIDATION TOGGLES ───────────────────────────────────────────────────
    # CHECK_DATA_TYPES is inherited from _Shared.
    CHECK_TABLES = True
    CHECK_COLUMNS = True
    CHECK_NULLABILITY = True
    CHECK_DEFAULT_VALUES = True
    CHECK_PRIMARY_KEYS = True
    CHECK_FOREIGN_KEYS = True
    CHECK_INDEXES = True
    CHECK_COLUMN_ORDER = False   # Column order differences are usually harmless

    # Normalize data types before comparing (e.g. INT ≈ INTEGER)
    NORMALIZE_DATA_TYPES = True

# # Defines how ERwin data is interpreted for different export formats; normally handled automatically by the parser.
# This setting maps ERwin's null-option code to "is NOT NULL?" — keep it separate from the LDM setting, which asks "is nullable?"

    ERWIN_ATTR_NULL_OPTION_CODES = {"1": True, "0": False, "2": False}

# # ERwin automatically creates foreign-key indexes for relationships, while PowerDesigner does not export them.
# They are ignored by default to avoid false index differences; set False to compare them.
    ERWIN_IGNORE_FK_INDEXES = True

# # PowerDesigner may use abstract types like "Enum" that ERwin converts into a concrete SQL type such as CHAR(18).
# These types are reported as INFO (DATA_TYPE_ABSTRACT) instead of being treated as critical data-type mismatches.

    PD_ABSTRACT_TYPES = {"ENUM"}

    # PowerDesigner auto-creates a backing index for every primary key,
    # alternate key and foreign key (linked via <c:LinkedObject>). These mirror
    # keys/FKs already validated elsewhere, and erwin does not export them as
    # indexes — comparing them yields hundreds of phantom "index only in
    # PowerDesigner" rows. True skips them; set False to compare them.
    PD_IGNORE_KEY_FK_INDEXES = True

    # ─── FINDING SEVERITY ─────────────────────────────────────────────────────

    FINDING_SEVERITY = {
        "TABLE_MISSING":       "CRITICAL",   # table in PD, absent in erwin
        "TABLE_EXTRA":         "WARNING",    # table in erwin, absent in PD
        "COLUMN_MISSING":      "CRITICAL",   # column in PD, absent in erwin
        "COLUMN_EXTRA":        "WARNING",    # column in erwin, absent in PD
        "DATA_TYPE":           "CRITICAL",   # different base data type
        "DATA_TYPE_LENGTH":    "WARNING",    # same base type, different length/precision
        "DATA_TYPE_ABSTRACT":  "INFO",       # PD abstract type vs concrete erwin type
        "NULLABILITY":         "INFO",       # NULL vs NOT NULL
        "DEFAULT":             "INFO",       # different default value
        "PRIMARY_KEY":         "CRITICAL",   # different PK columns / PK dropped in erwin
        "PRIMARY_KEY_PD_ONLY": "INFO",       # PK only on the PD side
        "FOREIGN_KEY_MISSING": "WARNING",    # FK in PD, absent in erwin
        "FOREIGN_KEY_EXTRA":   "INFO",       # FK in erwin, absent in PD
        "INDEX_MISSING":       "INFO",       # index in PD, absent in erwin
        "INDEX_EXTRA":         "INFO",       # index in erwin, absent in PD
    }

    # Below this the model is flagged "Review? = YES".
    # Deliberately lower than the 95.0 CDM and LDM use: a physical model carries
    # far more comparable objects, so the same absolute defect count scores
    # differently.
    FIDELITY_REVIEW_THRESHOLD = 90.0


# ══════════════════════════════════════════════════════════════════════════════
#  RUNTIME TIER VIEWS
#
#  Each engine imports one of the three objects below. They flatten a tier's
#  inheritance chain into plain writable attributes, so engine code keeps
#  working unchanged:
#
#      config.CHECK_ENTITIES          attribute read
#      config.as_dict()               the workbook's CONFIG sheet
#      config.resolve_severity(...)   CDM / LDM severity policy
#      config.REPORT_FILENAME = ...   the PDM bridge's runtime rename
# ══════════════════════════════════════════════════════════════════════════════
class _TierConfig:
    """One tier's settings, resolved through its inheritance chain."""

    def __init__(self, tier: str, source: type):
        self._tier = tier
        # reversed(__mro__) applies the most general class first, so a subclass
        # override lands last and wins.
        for klass in reversed(source.__mro__):
            for name, value in vars(klass).items():
                if name.isupper() and not name.startswith("_"):
                    setattr(self, name, value)

    def __repr__(self) -> str:
        return f"<{self._tier} config: {len(self.as_dict())} settings>"

    def as_dict(self) -> dict:
        """
        Every public setting for THIS tier only.

        Written into the report's CONFIG sheet so a reviewer can always tell
        which rules produced a given result. Because each tier view holds only
        its own chain, the LDM workbook shows LDM settings and nothing from CDM
        or PDM.
        """
        return {name: value for name, value in vars(self).items()
                if name.isupper() and not name.startswith("_")}

    def resolve_severity(self, category: str, default: str) -> str:
        """
        Apply SEVERITY_OVERRIDES to a finding category.
        Returns "IGNORE" when the category has been switched off entirely.
        """
        return getattr(self, "SEVERITY_OVERRIDES", {}).get(category, default)


CDM_CONFIG = _TierConfig("CDM", CDM)
LDM_CONFIG = _TierConfig("LDM", LDM)
PDM_CONFIG = _TierConfig("PDM", PDM)

_TIERS = {"CDM": CDM_CONFIG, "LDM": LDM_CONFIG, "PDM": PDM_CONFIG}


def tier(name: str) -> _TierConfig:
    """Look a tier view up by name — 'CDM', 'LDM' or 'PDM'."""
    key = name.upper()
    if key not in _TIERS:
        raise ValueError(f"Unknown tier {name!r}; expected one of {sorted(_TIERS)}")
    return _TIERS[key]