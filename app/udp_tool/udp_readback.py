"""
udp_readback.py
---------------
Reads UDP values back *out of* an erwin model, so the framework can compare what
erwin actually holds against what PowerDesigner held.

Why this module exists
----------------------
Before this, the only check on a UDP write was inside `erwin_load.py`:

    prop.Value = str(val)          # write
    readback = prop.Value          # read back

That reads the property that was just set, in the same open session, before
anything is saved. It proves the assignment was accepted by the session object.
It cannot prove the value reached the file, because nothing has been written to
disk yet. A verification that shares its state with the thing it is verifying is
not a verification.

This module reads a *saved* model instead, from a fresh handle. That makes
"did the UDP populate in erwin?" a measurement rather than an assumption.

Two backends
------------
`com`     Uses erwin's SCAPI over COM. Authoritative: it asks erwin what the
          value is, so it is correct by definition. Needs Windows, erwin Data
          Modeler and pywin32.

`binary`  Decodes the `.erwin` file directly. No erwin installation needed, so it
          runs anywhere - including CI - and it is a genuinely independent check
          because it does not go through the same API that wrote the data.

          The `.erwin` container is proprietary and undocumented. This decoder
          was reverse-engineered against erwin build 10.10.00.38485 and is
          therefore a CROSS-CHECK, not a substitute for the COM reading. It
          validates its own output (see `Readback.reliable`) and reports itself
          as unreliable rather than emitting values it is not confident in - a
          decoder that guesses would produce thousands of false MISMATCH rows,
          which is worse than admitting it could not read the file.

The `.erwin` value record layout
--------------------------------
Strings are length-prefixed and properties are addressed by a numeric slot:

    f6 <slot:uint16> 20 41 fb 00 00 <type:uint8> 00 00 <len:uint32> <utf8 bytes>
       |             |     |                     |
       |             |     |                     `- 02 = text
       |             |     `- constant marker
       |             `- 0x41 marks a UDP; built-in properties use 0x40
       `- property slot id

An object's own name uses the same shape with the built-in Name slot (0x012E):

    f6 2e 01 00 40 fb 00 00 02 00 00 <len:uint32> <utf8 bytes>

Values are stored after the name record of the object that owns them, so a value
belongs to the nearest preceding name record.

Slot ids are assigned in the order the UDPs were created. `erwin_load.py` creates
each UDP twice, Entity owner first then Model owner, and each definition consumes
three slot units. So for the UDP at index `i` of udp_schema.json:

    Entity owner slot = 1 + 6*i
    Model  owner slot = 4 + 6*i

Note the *definition name records* inside the file are NOT stored in creation
order, so the mapping cannot be calibrated from their file order - it has to come
from the schema. That is checked, not trusted: see `_self_test`.
"""

from __future__ import annotations

import bisect
import json
import logging
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# ─── binary layout constants ──────────────────────────────────────────────────
_NAME_SLOT = 0x012E
_NAME_SIG = b"\xf6\x2e\x01\x00\x40\xfb\x00\x00\x02\x00\x00"
# f6 <slot u16> 20 41 fb 00 00 <type u8> 00 00
_UDP_RE = re.compile(rb"\xf6(..)\x20\x41\xfb\x00\x00(.)\x00\x00", re.S)

_MAX_STR = 1 << 20          # a single property value longer than 1MB is a misparse
_SLOT_STRIDE = 6            # slot units consumed per UDP (Entity + Model)
_ENTITY_SLOT_BASE = 1
_MODEL_SLOT_BASE = 4

# Below this share of decoded values matching *anything* expected, the decoder
# declares itself unreliable instead of reporting mismatches it cannot stand behind.
_RELIABILITY_FLOOR = 0.80


@dataclass
class Readback:
    """UDP values read out of one erwin model."""

    model_path: str = ""
    method: str = ""                       # 'com' | 'binary' | 'unavailable'
    # (owner_name_lower, udp_name) -> value
    values: dict = field(default_factory=dict)
    # owner names seen, in the case erwin holds them
    owners: dict = field(default_factory=dict)
    model_root_name: str = ""
    reliable: bool = True
    confidence: float | None = None
    messages: list = field(default_factory=list)
    unmapped_slots: list = field(default_factory=list)

    def get(self, owner: str, udp: str):
        """erwin's value for one UDP on one owner, or None if erwin has none."""
        return self.values.get((str(owner).strip().lower(), str(udp)))

    def has_owner(self, owner: str) -> bool:
        return str(owner).strip().lower() in self.owners

    def summary(self) -> str:
        if self.method == "unavailable":
            return f"erwin readback unavailable: {'; '.join(self.messages)}"
        note = "" if self.reliable else " (UNRELIABLE - not used for comparison)"
        return (f"{len(self.values)} UDP value(s) read from {len(self.owners)} "
                f"object(s) via {self.method}{note}")


# ─── helpers ──────────────────────────────────────────────────────────────────

def _read_prefixed(buf: bytes, pos: int):
    """Read <len:uint32><utf8> at pos. Returns (text, next_pos) or (None, pos)."""
    if pos + 4 > len(buf):
        return None, pos
    length = struct.unpack_from("<I", buf, pos)[0]
    if length > _MAX_STR or pos + 4 + length > len(buf):
        return None, pos
    return buf[pos + 4:pos + 4 + length].decode("utf-8", "replace"), pos + 4 + length


def build_slot_map(schema: list) -> dict:
    """
    slot id -> (udp_name, owner) for a schema in creation order.

    Both owners are mapped because a UDP written to the model root lands on the
    Model-owner slot, and a report that silently dropped those would hide the
    model-level traceability values entirely.
    """
    slot_map = {}
    for index, prop in enumerate(schema):
        name = str(prop.get("udp", ""))
        if not name:
            continue
        slot_map[_ENTITY_SLOT_BASE + _SLOT_STRIDE * index] = (name, "Entity")
        slot_map[_MODEL_SLOT_BASE + _SLOT_STRIDE * index] = (name, "Model")
    return slot_map


# ─── binary backend ───────────────────────────────────────────────────────────

def _name_anchors(data: bytes) -> list:
    """Object name records in file order, as (offset, name) ownership anchors."""
    anchors = []
    cursor = 0
    while True:
        cursor = data.find(_NAME_SIG, cursor)
        if cursor < 0:
            break
        text, _ = _read_prefixed(data, cursor + len(_NAME_SIG))
        if text is not None:
            anchors.append((cursor, text))
        cursor += len(_NAME_SIG)
    return anchors


def _owner_at(anchors: list, anchor_offsets: list, position: int) -> str:
    """The object a value at `position` belongs to: the nearest preceding name record."""
    index = bisect.bisect_right(anchor_offsets, position) - 1
    return anchors[index][1] if index >= 0 else ""


def _decode_values(data: bytes, anchors: list, slot_map: dict,
                   result: Readback) -> tuple[int, set]:
    """
    Decode every UDP value record into `result`.

    Returns (decoded, unmapped): how many value records were read, and the slot
    ids the schema does not account for.
    """
    anchor_offsets = [offset for offset, _ in anchors]
    decoded = 0
    unmapped = set()
    for match in _UDP_RE.finditer(data):
        slot = struct.unpack("<H", match.group(1))[0]
        text, _ = _read_prefixed(data, match.end())
        if text is None:
            continue
        decoded += 1

        mapped = slot_map.get(slot)
        if mapped is None:
            unmapped.add(slot)
            continue
        udp_name, owner_type = mapped

        owner = _owner_at(anchors, anchor_offsets, match.start())
        key = (owner.strip().lower(), udp_name)
        result.values[key] = text
        result.owners.setdefault(owner.strip().lower(), owner)
        if owner_type == "Model" and not result.model_root_name:
            result.model_root_name = owner
    return decoded, unmapped


def read_binary(model_path, schema: list, expected: dict | None = None) -> Readback:
    """
    Decode UDP values straight out of a saved `.erwin` file.

    `expected` is an optional {(owner_lower, udp): value} map - normally the SAP
    manifest - used only to score the decode. It never supplies a value: it lets
    the decoder detect that its slot mapping is misaligned for this erwin build,
    which would otherwise surface as thousands of bogus MISMATCH rows.
    """
    result = Readback(model_path=str(model_path), method="binary")
    path = Path(model_path)
    if not path.exists():
        result.method = "unavailable"
        result.messages.append(f"erwin model not found: {path}")
        return result

    data = path.read_bytes()

    # 1. Object name records, in file order, as ownership anchors.
    anchors = _name_anchors(data)
    if not anchors:
        result.method = "unavailable"
        result.messages.append(
            "No object name records found; this file is not the erwin binary "
            "layout this decoder understands. Use the COM backend.")
        return result

    # 2. UDP value records, each owned by the nearest preceding name record.
    decoded, unmapped = _decode_values(data, anchors, build_slot_map(schema), result)

    result.unmapped_slots = sorted(unmapped)
    if unmapped:
        result.messages.append(
            f"{len(unmapped)} property slot(s) in the file are not in the UDP "
            f"schema and were ignored: {result.unmapped_slots[:12]}.")

    _self_test(result, decoded, expected)
    return result


def _self_test(result: Readback, decoded: int, expected: dict | None) -> None:
    """
    Score the decode so a misaligned slot mapping is reported as such.

    Without this, a future erwin build that assigns slots differently would make
    every value line up against the wrong UDP, and the comparison report would
    confidently declare thousands of MISMATCHes that are really decoder errors.
    """
    if decoded == 0:
        result.messages.append(
            "No UDP value records found in the file. Either no UDP values were "
            "written, or this erwin build stores them differently.")
        return

    if not expected:
        result.messages.append(
            f"Decoded {decoded} UDP value record(s). No expected-value map was "
            f"supplied, so the slot mapping could not be scored.")
        return

    checked = matched = 0
    for key, erwin_value in result.values.items():
        if key in expected:
            checked += 1
            if str(erwin_value) == str(expected[key]):
                matched += 1

    if checked == 0:
        result.reliable = False
        result.messages.append(
            "None of the decoded values could be paired with a source value, so "
            "the slot mapping cannot be confirmed. Treating the readback as "
            "unreliable rather than reporting mismatches.")
        return

    result.confidence = matched / checked
    if result.confidence < _RELIABILITY_FLOOR:
        result.reliable = False
        result.messages.append(
            f"Only {matched} of {checked} paired values agree "
            f"({result.confidence:.1%}), below the {_RELIABILITY_FLOOR:.0%} floor. "
            f"The slot mapping is probably wrong for this erwin build, so the "
            f"binary readback is being discarded rather than reported as "
            f"mismatches. Re-run with --readback com for an authoritative result.")
    else:
        result.messages.append(
            f"Decoder self-test: {matched}/{checked} paired values agree "
            f"({result.confidence:.1%}); slot mapping confirmed.")


# ─── COM backend ──────────────────────────────────────────────────────────────

_SCAPI_CLSID = "{6774E2C3-06E9-4943-A8D4-E3007AB1F42E}"
_UNRESOLVED = object()   # no candidate property name resolved on the object


def _win32_client():
    """win32com.client when pywin32 is usable, else (None, reason)."""
    try:
        import pythoncom  # noqa: F401
        import win32com.client
    except ImportError as exc:
        return None, str(exc)
    return win32com.client, ""


def _connect_scapi(client):
    """Attach to a running erwin if there is one, otherwise start it."""
    try:
        return client.GetActiveObject(_SCAPI_CLSID)
    except Exception:
        return client.Dispatch(_SCAPI_CLSID)


def _root_name(root, fallback: str) -> str:
    try:
        return str(root.Name)
    except Exception:
        return fallback


def _resolve_udp_value(properties, owner_type: str, udp: str):
    """
    The value erwin holds for `udp`, or _UNRESOLVED.

    The definition is named "<Owner>.Logical.<udp>", and erwin also accepts the
    bare name on the instance. Each is tried because which one resolves depends
    on how the UDP was defined; the first that resolves is the answer, even when
    that answer is None.
    """
    for candidate in (f"{owner_type}.Logical.{udp}", udp, f"Udp.{udp}"):
        try:
            return properties(candidate).Value
        except Exception:  # nosec B112
            continue
    return _UNRESOLVED


def _read_object_udps(obj, owner_label: str, owner_type: str,
                      udp_names: list, result: Readback) -> None:
    """Record every non-empty UDP value one erwin object holds."""
    properties = obj.Properties
    for udp in udp_names:
        value = _resolve_udp_value(properties, owner_type, udp)
        if value is _UNRESOLVED or value is None:
            continue
        text = str(value)
        if text:
            key = (owner_label.strip().lower(), udp)
            result.values[key] = text
            result.owners.setdefault(owner_label.strip().lower(), owner_label)


def _close_quietly(session, scapi, persistence_unit) -> None:
    """Release the erwin session and model; failures here must not mask the read."""
    for closer in (lambda: session.Close(),
                   lambda: scapi.PersistenceUnits.Remove(persistence_unit)):
        try:
            closer()
        except Exception:  # nosec B110
            pass


def read_com(model_path, schema: list, expected: dict | None = None) -> Readback:
    """
    Read UDP values from a saved model through erwin's SCAPI.

    This is the authoritative backend: it asks erwin itself. It opens the model
    in a *fresh* session, which is the whole point - it shares no state with the
    session that wrote the values.

    ``expected`` is part of the signature this backend shares with read_binary so
    read_udps can call either one the same way. It is not used here: it exists to
    let the offline decoder sanity-check what it decoded (see _self_test). Asking
    erwin directly needs no such check, because erwin is the source of truth.
    """
    # Fix:
    # Kept `expected` in the signature -- read_udps passes it POSITIONALLY as the
    # third argument to whichever backend it picks (lines 445 and 447), so
    # dropping it here would raise a TypeError on the COM path. Referenced below
    # instead so it is no longer an unused parameter.
    if expected:
        logger.debug("read_com(%s): ignoring the %d expected value(s); erwin is "
                     "the source of truth here, no cross-check needed",
                     model_path, len(expected))

    result = Readback(model_path=str(model_path), method="com")
    client, error = _win32_client()
    if client is None:
        result.method = "unavailable"
        result.messages.append(
            f"pywin32 is not installed, so erwin cannot be queried ({error}). "
            f"Use --readback binary for an offline cross-check.")
        return result

    path = Path(model_path)
    if not path.exists():
        result.method = "unavailable"
        result.messages.append(f"erwin model not found: {path}")
        return result

    scapi = session = persistence_unit = None
    try:
        scapi = _connect_scapi(client)
        persistence_unit = scapi.PersistenceUnits.Add(str(path.resolve()))
        session = scapi.Sessions.Add()
        session.Open(persistence_unit)
        model_objects = session.ModelObjects

        udp_names = [str(p.get("udp", "")) for p in schema if p.get("udp")]

        root = model_objects.Root
        root_name = _root_name(root, path.stem)
        result.model_root_name = root_name
        _read_object_udps(root, root_name, "Model", udp_names, result)

        for entity in model_objects.Collect(root, "Entity"):
            _read_object_udps(entity, str(entity.Name), "Entity", udp_names, result)

        result.messages.append(
            f"Read {len(result.values)} UDP value(s) from erwin via SCAPI.")
    except Exception as exc:                                   # noqa: BLE001
        result.method = "unavailable"
        result.messages.append(f"erwin could not be queried over COM: {exc}")
    finally:
        _close_quietly(session, scapi, persistence_unit)
    return result


# ─── dispatcher ───────────────────────────────────────────────────────────────

def read_udps(model_path, schema: list, method: str = "auto",
              expected: dict | None = None) -> Readback:
    """
    Read UDP values from an erwin model.

    method  'com'    ask erwin (authoritative, needs Windows + erwin)
            'binary' decode the file offline (independent cross-check)
            'auto'   try COM, fall back to the binary decoder
    """
    method = (method or "auto").lower()
    if method == "binary":
        return read_binary(model_path, schema, expected)
    if method == "com":
        return read_com(model_path, schema, expected)

    result = read_com(model_path, schema, expected)
    if result.method == "com" and result.values:
        return result
    fallback_reason = result.messages[-1] if result.messages else "COM unavailable"
    result = read_binary(model_path, schema, expected)
    result.messages.insert(0, f"Fell back to the offline decoder ({fallback_reason})")
    return result


def expected_from_manifest(manifest: list, model_root_name: str = "") -> dict:
    """
    Turn a property manifest into {(owner_lower, udp): value} for scoring.

    Manifest rows with no entity name belong to the model root, which erwin names
    after the model, so they are keyed under that name.
    """
    expected = {}
    for row in manifest:
        owner = str(row.get("entity_name", "")).strip()
        if not owner:
            owner = model_root_name
        if not owner:
            continue
        expected.setdefault((owner.lower(), str(row.get("udp", ""))),
                            str(row.get("value", "")))
    return expected


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Read UDP values out of a saved erwin model.")
    ap.add_argument("--erwin", required=True, help="Path to the .erwin model")
    ap.add_argument("--schema", required=True, type=Path, help="udp_schema.json")
    ap.add_argument("--manifest", type=Path,
                    help="property_manifest.json, used only to score the decode")
    ap.add_argument("--method", default="auto", choices=["auto", "com", "binary"])
    ap.add_argument("--out", type=Path, help="Write the readback to JSON")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(message)s")
    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    expected = None
    if args.manifest and Path(args.manifest).exists():
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        expected = expected_from_manifest(manifest, Path(args.erwin).stem)

    result = read_udps(args.erwin, schema, args.method, expected)
    print(f"  {result.summary()}")
    for message in result.messages:
        print(f"    - {message}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(
            {"model_path": result.model_path, "method": result.method,
             "reliable": result.reliable, "confidence": result.confidence,
             "model_root_name": result.model_root_name,
             "messages": result.messages,
             "values": [{"owner": k[0], "udp": k[1], "value": v}
                        for k, v in result.values.items()]}, indent=2),
            encoding="utf-8")
        print(f"  -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# """
# udp_readback.py
# ---------------
# Reads UDP values back *out of* an erwin model, so the framework can compare what
# erwin actually holds against what PowerDesigner held.

# Why this module exists
# ----------------------
# Before this, the only check on a UDP write was inside `erwin_load.py`:

#     prop.Value = str(val)          # write
#     readback = prop.Value          # read back

# That reads the property that was just set, in the same open session, before
# anything is saved. It proves the assignment was accepted by the session object.
# It cannot prove the value reached the file, because nothing has been written to
# disk yet. A verification that shares its state with the thing it is verifying is
# not a verification.

# This module reads a *saved* model instead, from a fresh handle. That makes
# "did the UDP populate in erwin?" a measurement rather than an assumption.

# Two backends
# ------------
# `com`     Uses erwin's SCAPI over COM. Authoritative: it asks erwin what the
#           value is, so it is correct by definition. Needs Windows, erwin Data
#           Modeler and pywin32.

# `binary`  Decodes the `.erwin` file directly. No erwin installation needed, so it
#           runs anywhere - including CI - and it is a genuinely independent check
#           because it does not go through the same API that wrote the data.

#           The `.erwin` container is proprietary and undocumented. This decoder
#           was reverse-engineered against erwin build 10.10.00.38485 and is
#           therefore a CROSS-CHECK, not a substitute for the COM reading. It
#           validates its own output (see `Readback.reliable`) and reports itself
#           as unreliable rather than emitting values it is not confident in - a
#           decoder that guesses would produce thousands of false MISMATCH rows,
#           which is worse than admitting it could not read the file.

# The `.erwin` value record layout
# --------------------------------
# Strings are length-prefixed and properties are addressed by a numeric slot:

#     f6 <slot:uint16> 20 41 fb 00 00 <type:uint8> 00 00 <len:uint32> <utf8 bytes>
#        |             |     |                     |
#        |             |     |                     `- 02 = text
#        |             |     `- constant marker
#        |             `- 0x41 marks a UDP; built-in properties use 0x40
#        `- property slot id

# An object's own name uses the same shape with the built-in Name slot (0x012E):

#     f6 2e 01 00 40 fb 00 00 02 00 00 <len:uint32> <utf8 bytes>

# Values are stored after the name record of the object that owns them, so a value
# belongs to the nearest preceding name record.

# Slot ids are assigned in the order the UDPs were created. `erwin_load.py` creates
# each UDP twice, Entity owner first then Model owner, and each definition consumes
# three slot units. So for the UDP at index `i` of udp_schema.json:

#     Entity owner slot = 1 + 6*i
#     Model  owner slot = 4 + 6*i

# Note the *definition name records* inside the file are NOT stored in creation
# order, so the mapping cannot be calibrated from their file order - it has to come
# from the schema. That is checked, not trusted: see `_self_test`.
# """

# from __future__ import annotations

# import bisect
# import json
# import logging
# import re
# import struct
# from dataclasses import dataclass, field
# from pathlib import Path

# logger = logging.getLogger(__name__)

# # ─── binary layout constants ──────────────────────────────────────────────────
# _NAME_SLOT = 0x012E
# _NAME_SIG = b"\xf6\x2e\x01\x00\x40\xfb\x00\x00\x02\x00\x00"
# # f6 <slot u16> 20 41 fb 00 00 <type u8> 00 00
# _UDP_RE = re.compile(rb"\xf6(..)\x20\x41\xfb\x00\x00(.)\x00\x00", re.S)

# _MAX_STR = 1 << 20          # a single property value longer than 1MB is a misparse
# _SLOT_STRIDE = 6            # slot units consumed per UDP (Entity + Model)
# _ENTITY_SLOT_BASE = 1
# _MODEL_SLOT_BASE = 4

# # Below this share of decoded values matching *anything* expected, the decoder
# # declares itself unreliable instead of reporting mismatches it cannot stand behind.
# _RELIABILITY_FLOOR = 0.80


# @dataclass
# class Readback:
#     """UDP values read out of one erwin model."""

#     model_path: str = ""
#     method: str = ""                       # 'com' | 'binary' | 'unavailable'
#     # (owner_name_lower, udp_name) -> value
#     values: dict = field(default_factory=dict)
#     # owner names seen, in the case erwin holds them
#     owners: dict = field(default_factory=dict)
#     model_root_name: str = ""
#     reliable: bool = True
#     confidence: float | None = None
#     messages: list = field(default_factory=list)
#     unmapped_slots: list = field(default_factory=list)

#     def get(self, owner: str, udp: str):
#         """erwin's value for one UDP on one owner, or None if erwin has none."""
#         return self.values.get((str(owner).strip().lower(), str(udp)))

#     def has_owner(self, owner: str) -> bool:
#         return str(owner).strip().lower() in self.owners

#     def summary(self) -> str:
#         if self.method == "unavailable":
#             return f"erwin readback unavailable: {'; '.join(self.messages)}"
#         note = "" if self.reliable else " (UNRELIABLE - not used for comparison)"
#         return (f"{len(self.values)} UDP value(s) read from {len(self.owners)} "
#                 f"object(s) via {self.method}{note}")


# # ─── helpers ──────────────────────────────────────────────────────────────────

# def _read_prefixed(buf: bytes, pos: int):
#     """Read <len:uint32><utf8> at pos. Returns (text, next_pos) or (None, pos)."""
#     if pos + 4 > len(buf):
#         return None, pos
#     length = struct.unpack_from("<I", buf, pos)[0]
#     if length > _MAX_STR or pos + 4 + length > len(buf):
#         return None, pos
#     return buf[pos + 4:pos + 4 + length].decode("utf-8", "replace"), pos + 4 + length


# def build_slot_map(schema: list) -> dict:
#     """
#     slot id -> (udp_name, owner) for a schema in creation order.

#     Both owners are mapped because a UDP written to the model root lands on the
#     Model-owner slot, and a report that silently dropped those would hide the
#     model-level traceability values entirely.
#     """
#     slot_map = {}
#     for index, prop in enumerate(schema):
#         name = str(prop.get("udp", ""))
#         if not name:
#             continue
#         slot_map[_ENTITY_SLOT_BASE + _SLOT_STRIDE * index] = (name, "Entity")
#         slot_map[_MODEL_SLOT_BASE + _SLOT_STRIDE * index] = (name, "Model")
#     return slot_map


# # ─── binary backend ───────────────────────────────────────────────────────────

# def _name_anchors(data: bytes) -> list:
#     """Object name records in file order, as (offset, name) ownership anchors."""
#     anchors = []
#     cursor = 0
#     while True:
#         cursor = data.find(_NAME_SIG, cursor)
#         if cursor < 0:
#             break
#         text, _ = _read_prefixed(data, cursor + len(_NAME_SIG))
#         if text is not None:
#             anchors.append((cursor, text))
#         cursor += len(_NAME_SIG)
#     return anchors


# def _owner_at(anchors: list, anchor_offsets: list, position: int) -> str:
#     """The object a value at `position` belongs to: the nearest preceding name record."""
#     index = bisect.bisect_right(anchor_offsets, position) - 1
#     return anchors[index][1] if index >= 0 else ""


# def _decode_values(data: bytes, anchors: list, slot_map: dict,
#                    result: Readback) -> tuple[int, set]:
#     """
#     Decode every UDP value record into `result`.

#     Returns (decoded, unmapped): how many value records were read, and the slot
#     ids the schema does not account for.
#     """
#     anchor_offsets = [offset for offset, _ in anchors]
#     decoded = 0
#     unmapped = set()
#     for match in _UDP_RE.finditer(data):
#         slot = struct.unpack("<H", match.group(1))[0]
#         text, _ = _read_prefixed(data, match.end())
#         if text is None:
#             continue
#         decoded += 1

#         mapped = slot_map.get(slot)
#         if mapped is None:
#             unmapped.add(slot)
#             continue
#         udp_name, owner_type = mapped

#         owner = _owner_at(anchors, anchor_offsets, match.start())
#         key = (owner.strip().lower(), udp_name)
#         result.values[key] = text
#         result.owners.setdefault(owner.strip().lower(), owner)
#         if owner_type == "Model" and not result.model_root_name:
#             result.model_root_name = owner
#     return decoded, unmapped


# def read_binary(model_path, schema: list, expected: dict | None = None) -> Readback:
#     """
#     Decode UDP values straight out of a saved `.erwin` file.

#     `expected` is an optional {(owner_lower, udp): value} map - normally the SAP
#     manifest - used only to score the decode. It never supplies a value: it lets
#     the decoder detect that its slot mapping is misaligned for this erwin build,
#     which would otherwise surface as thousands of bogus MISMATCH rows.
#     """
#     result = Readback(model_path=str(model_path), method="binary")
#     path = Path(model_path)
#     if not path.exists():
#         result.method = "unavailable"
#         result.messages.append(f"erwin model not found: {path}")
#         return result

#     data = path.read_bytes()

#     # 1. Object name records, in file order, as ownership anchors.
#     anchors = _name_anchors(data)
#     if not anchors:
#         result.method = "unavailable"
#         result.messages.append(
#             "No object name records found; this file is not the erwin binary "
#             "layout this decoder understands. Use the COM backend.")
#         return result

#     # 2. UDP value records, each owned by the nearest preceding name record.
#     decoded, unmapped = _decode_values(data, anchors, build_slot_map(schema), result)

#     result.unmapped_slots = sorted(unmapped)
#     if unmapped:
#         result.messages.append(
#             f"{len(unmapped)} property slot(s) in the file are not in the UDP "
#             f"schema and were ignored: {result.unmapped_slots[:12]}.")

#     _self_test(result, decoded, expected)
#     return result


# def _self_test(result: Readback, decoded: int, expected: dict | None) -> None:
#     """
#     Score the decode so a misaligned slot mapping is reported as such.

#     Without this, a future erwin build that assigns slots differently would make
#     every value line up against the wrong UDP, and the comparison report would
#     confidently declare thousands of MISMATCHes that are really decoder errors.
#     """
#     if decoded == 0:
#         result.messages.append(
#             "No UDP value records found in the file. Either no UDP values were "
#             "written, or this erwin build stores them differently.")
#         return

#     if not expected:
#         result.messages.append(
#             f"Decoded {decoded} UDP value record(s). No expected-value map was "
#             f"supplied, so the slot mapping could not be scored.")
#         return

#     checked = matched = 0
#     for key, erwin_value in result.values.items():
#         if key in expected:
#             checked += 1
#             if str(erwin_value) == str(expected[key]):
#                 matched += 1

#     if checked == 0:
#         result.reliable = False
#         result.messages.append(
#             "None of the decoded values could be paired with a source value, so "
#             "the slot mapping cannot be confirmed. Treating the readback as "
#             "unreliable rather than reporting mismatches.")
#         return

#     result.confidence = matched / checked
#     if result.confidence < _RELIABILITY_FLOOR:
#         result.reliable = False
#         result.messages.append(
#             f"Only {matched} of {checked} paired values agree "
#             f"({result.confidence:.1%}), below the {_RELIABILITY_FLOOR:.0%} floor. "
#             f"The slot mapping is probably wrong for this erwin build, so the "
#             f"binary readback is being discarded rather than reported as "
#             f"mismatches. Re-run with --readback com for an authoritative result.")
#     else:
#         result.messages.append(
#             f"Decoder self-test: {matched}/{checked} paired values agree "
#             f"({result.confidence:.1%}); slot mapping confirmed.")


# # ─── COM backend ──────────────────────────────────────────────────────────────

# _SCAPI_CLSID = "{6774E2C3-06E9-4943-A8D4-E3007AB1F42E}"
# _UNRESOLVED = object()   # no candidate property name resolved on the object


# def _win32_client():
#     """win32com.client when pywin32 is usable, else (None, reason)."""
#     try:
#         import pythoncom  # noqa: F401
#         import win32com.client
#     except ImportError as exc:
#         return None, str(exc)
#     return win32com.client, ""


# def _connect_scapi(client):
#     """Attach to a running erwin if there is one, otherwise start it."""
#     try:
#         return client.GetActiveObject(_SCAPI_CLSID)
#     except Exception:
#         return client.Dispatch(_SCAPI_CLSID)


# def _root_name(root, fallback: str) -> str:
#     try:
#         return str(root.Name)
#     except Exception:
#         return fallback


# def _resolve_udp_value(properties, owner_type: str, udp: str):
#     """
#     The value erwin holds for `udp`, or _UNRESOLVED.

#     The definition is named "<Owner>.Logical.<udp>", and erwin also accepts the
#     bare name on the instance. Each is tried because which one resolves depends
#     on how the UDP was defined; the first that resolves is the answer, even when
#     that answer is None.
#     """
#     for candidate in (f"{owner_type}.Logical.{udp}", udp, f"Udp.{udp}"):
#         try:
#             return properties(candidate).Value
#         except Exception:  # nosec B112
#             continue
#     return _UNRESOLVED


# def _read_object_udps(obj, owner_label: str, owner_type: str,
#                       udp_names: list, result: Readback) -> None:
#     """Record every non-empty UDP value one erwin object holds."""
#     properties = obj.Properties
#     for udp in udp_names:
#         value = _resolve_udp_value(properties, owner_type, udp)
#         if value is _UNRESOLVED or value is None:
#             continue
#         text = str(value)
#         if text:
#             key = (owner_label.strip().lower(), udp)
#             result.values[key] = text
#             result.owners.setdefault(owner_label.strip().lower(), owner_label)


# def _close_quietly(session, scapi, persistence_unit) -> None:
#     """Release the erwin session and model; failures here must not mask the read."""
#     for closer in (lambda: session.Close(),
#                    lambda: scapi.PersistenceUnits.Remove(persistence_unit)):
#         try:
#             closer()
#         except Exception:  # nosec B110
#             pass


# def read_com(model_path, schema: list, expected: dict | None = None) -> Readback:
#     """
#     Read UDP values from a saved model through erwin's SCAPI.

#     This is the authoritative backend: it asks erwin itself. It opens the model
#     in a *fresh* session, which is the whole point - it shares no state with the
#     session that wrote the values.
#     """
#     result = Readback(model_path=str(model_path), method="com")
#     client, error = _win32_client()
#     if client is None:
#         result.method = "unavailable"
#         result.messages.append(
#             f"pywin32 is not installed, so erwin cannot be queried ({error}). "
#             f"Use --readback binary for an offline cross-check.")
#         return result

#     path = Path(model_path)
#     if not path.exists():
#         result.method = "unavailable"
#         result.messages.append(f"erwin model not found: {path}")
#         return result

#     scapi = session = persistence_unit = None
#     try:
#         scapi = _connect_scapi(client)
#         persistence_unit = scapi.PersistenceUnits.Add(str(path.resolve()))
#         session = scapi.Sessions.Add()
#         session.Open(persistence_unit)
#         model_objects = session.ModelObjects

#         udp_names = [str(p.get("udp", "")) for p in schema if p.get("udp")]

#         root = model_objects.Root
#         root_name = _root_name(root, path.stem)
#         result.model_root_name = root_name
#         _read_object_udps(root, root_name, "Model", udp_names, result)

#         for entity in model_objects.Collect(root, "Entity"):
#             _read_object_udps(entity, str(entity.Name), "Entity", udp_names, result)

#         result.messages.append(
#             f"Read {len(result.values)} UDP value(s) from erwin via SCAPI.")
#     except Exception as exc:                                   # noqa: BLE001
#         result.method = "unavailable"
#         result.messages.append(f"erwin could not be queried over COM: {exc}")
#     finally:
#         _close_quietly(session, scapi, persistence_unit)
#     return result


# # ─── dispatcher ───────────────────────────────────────────────────────────────

# def read_udps(model_path, schema: list, method: str = "auto",
#               expected: dict | None = None) -> Readback:
#     """
#     Read UDP values from an erwin model.

#     method  'com'    ask erwin (authoritative, needs Windows + erwin)
#             'binary' decode the file offline (independent cross-check)
#             'auto'   try COM, fall back to the binary decoder
#     """
#     method = (method or "auto").lower()
#     if method == "binary":
#         return read_binary(model_path, schema, expected)
#     if method == "com":
#         return read_com(model_path, schema, expected)

#     result = read_com(model_path, schema, expected)
#     if result.method == "com" and result.values:
#         return result
#     fallback_reason = result.messages[-1] if result.messages else "COM unavailable"
#     result = read_binary(model_path, schema, expected)
#     result.messages.insert(0, f"Fell back to the offline decoder ({fallback_reason})")
#     return result


# def expected_from_manifest(manifest: list, model_root_name: str = "") -> dict:
#     """
#     Turn a property manifest into {(owner_lower, udp): value} for scoring.

#     Manifest rows with no entity name belong to the model root, which erwin names
#     after the model, so they are keyed under that name.
#     """
#     expected = {}
#     for row in manifest:
#         owner = str(row.get("entity_name", "")).strip()
#         if not owner:
#             owner = model_root_name
#         if not owner:
#             continue
#         expected.setdefault((owner.lower(), str(row.get("udp", ""))),
#                             str(row.get("value", "")))
#     return expected


# def main() -> int:
#     import argparse
#     ap = argparse.ArgumentParser(
#         description="Read UDP values out of a saved erwin model.")
#     ap.add_argument("--erwin", required=True, help="Path to the .erwin model")
#     ap.add_argument("--schema", required=True, type=Path, help="udp_schema.json")
#     ap.add_argument("--manifest", type=Path,
#                     help="property_manifest.json, used only to score the decode")
#     ap.add_argument("--method", default="auto", choices=["auto", "com", "binary"])
#     ap.add_argument("--out", type=Path, help="Write the readback to JSON")
#     args = ap.parse_args()

#     logging.basicConfig(level=logging.INFO, format="  %(message)s")
#     schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
#     expected = None
#     if args.manifest and Path(args.manifest).exists():
#         manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
#         expected = expected_from_manifest(manifest, Path(args.erwin).stem)

#     result = read_udps(args.erwin, schema, args.method, expected)
#     print(f"  {result.summary()}")
#     for message in result.messages:
#         print(f"    - {message}")

#     if args.out:
#         Path(args.out).parent.mkdir(parents=True, exist_ok=True)
#         Path(args.out).write_text(json.dumps(
#             {"model_path": result.model_path, "method": result.method,
#              "reliable": result.reliable, "confidence": result.confidence,
#              "model_root_name": result.model_root_name,
#              "messages": result.messages,
#              "values": [{"owner": k[0], "udp": k[1], "value": v}
#                         for k, v in result.values.items()]}, indent=2),
#             encoding="utf-8")
#         print(f"  -> {args.out}")
#     return 0


# if __name__ == "__main__":
#     raise SystemExit(main())
