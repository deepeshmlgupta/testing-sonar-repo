"""
erwin_load.py
-------------
This script automates the process of adding custom properties (User-Defined Properties or UDPs) 
into an erwin Data Modeler file and filling them with data from an Excel/JSON file.

How it works (The 5 Steps):
1. Load Data: It reads your property rules (udp_schema) and data values (property_manifest) from the JSON files.
2. Connect to erwin: It hooks into an open erwin window or starts a background process to open your XML model.
3. Setup the Dictionary: It checks if your custom properties exist in erwin. If not, it creates them and turns on the hidden switches so they show up in the UI.
4. Inject Data: It rapidly loops through your tables (Entities) and inserts the Excel data into the properties.
5. Save & Close: It saves the updated XML file and safely closes the connection to prevent corruption.

It also records what actually happened. Every value is written and then read
back out of erwin, and the outcome of each write, along with every dictionary
create/update, is written to --results_json. udp_report.py turns that file into
the Excel migration report, so the report shows real results instead of
assuming the injection succeeded.

Usage Example:
  python erwin_load.py --xml "path/to/model.erwin" --manifest "path/to/property_manifest.json" --schema "path/to/udp_schema.json"
"""

import argparse
import json
import sys
import os
from datetime import datetime
from pathlib import Path

# Post-save verification. Re-reading the saved file is the only way to know a
# UDP value persisted; the in-session read-back further down shares state with
# the write that produced it, so it cannot detect a value that never reaches
# disk.
try:
    import udp_readback
except ImportError:
    udp_readback = None

# Status vocabulary shared with udp_report.py. Keep the two files in step -
# the Summary sheet counts these exact strings.
ST_VERIFIED = "Applied - Verified"
ST_ALTERED = "Applied - Value Altered"
ST_UNVERIFIED = "Applied - Not Verified"
ST_NO_ENTITY = "Skipped - Entity Not Found In erwin"
ST_REJECTED = "Skipped - Property Rejected By erwin"

# Fix:
# The --manifest / --schema defaults used to be bare relative paths, so they
# resolved against whatever folder you happened to run the script from. Running
# it from the project root looked for erwin_input\ at the root and failed.
# Anchor them to this file's own folder instead, so the defaults mean the same
# thing from any working directory. An explicit --manifest/--schema still wins
# and is still resolved the normal way.
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = SCRIPT_DIR / "erwin_input" / "property_manifest.json"
DEFAULT_SCHEMA = SCRIPT_DIR / "erwin_input" / "udp_schema.json"

try:
    import win32com.client
    import pythoncom
except ImportError:
    print("Error: The 'pywin32' tool is not installed. Please run 'pip install pywin32' in your terminal.")
    sys.exit(1)


def connect_scapi():
    """Connect to erwin. It tries to use an already open window first. If none is open, it starts a hidden one."""
    clsid = "{6774E2C3-06E9-4943-A8D4-E3007AB1F42E}"
    try:
        # Try to connect to the erwin window you currently have open
        scapi = win32com.client.GetActiveObject(clsid)
        return scapi, True
    except Exception as e:
        # print(f"Notice: Could not find an open erwin window. Starting a background process instead. (Error: {e})")
        try:
            # Fallback: Start a new, hidden background erwin process
            scapi = win32com.client.Dispatch(clsid)
            return scapi, False
        except pythoncom.com_error as e2:
            print(f"Error: Completely failed to launch erwin. Please make sure erwin is installed. (Error: {e2})")
            sys.exit(1)


def load_model(scapi, is_visible, xml_path=None):
    """Loads your erwin file. If you don't provide a file path, it tries to use whatever file is currently open."""
    pu = scapi.PersistenceUnits
    if xml_path:
        xml_path = Path(xml_path).resolve()
        if not xml_path.exists():
            print(f"Error: The XML file {xml_path} does not exist. Please check the path.")
            sys.exit(1)
            
        if not is_visible and xml_path.suffix.lower() == ".xml":
            # Fix:
            # Dropped the f prefix on these four prints -- none of them has a
            # {placeholder}, so the f was doing nothing. Text is unchanged.
            print("CRITICAL ERROR: You are trying to load an XML file, but the erwin UI is not open on your desktop!")
            print("Due to an erwin bug, loading XML files in the background causes a catastrophic crash.")
            print("Please double-click the erwin application to open it on your screen, then run the pipeline again.")
            sys.exit(1)
            
        print("       Connecting to erwin...")
        # erwin requires "erwin://" at the start of XML file paths
        if xml_path.suffix.lower() == ".xml":
            load_path = f"erwin://{xml_path}"
        else:
            load_path = str(xml_path)
            
        model = pu.Add(load_path)
    else:
        if pu.Count == 0:
            print("Error: No models are currently open in erwin. Please open a model or provide a file path using --xml.")
            sys.exit(1)
        # Grab the very first model that is currently open
        model = pu.Item(0)
        
    return model


def main():
    ap = argparse.ArgumentParser(description="Inject custom properties into an erwin model.")
    ap.add_argument("--xml", help="Path to the erwin XML model to load. If left blank, uses the active open model.")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to the data file (property_manifest.json)")
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA), help="Path to the rules file (udp_schema.json)")
    ap.add_argument("--out_erwin", help="Optional path to Save As an .erwin file.")
    ap.add_argument("--out_xml", help="Optional path to Save As an .xml file.")
    ap.add_argument("--results_json", help="Optional path to write the injection results.")
    ap.add_argument("--udp_name_style", default="qualified", choices=["qualified", "bare"], help="How UDP definitions are named in the erwin dictionary.")
    ap.add_argument("--no_verify", dest="verify", action="store_false", help="Skip re-opening the saved model to verify.")
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        # Fix:
        # Print the resolved absolute path. The old message showed the bare
        # relative string, which hid the fact that it had been resolved against
        # the current directory rather than the script's folder.
        print(f"Error: Data file {manifest_path.resolve()} was not found.")
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text())
    started = datetime.now()
    dictionary_log = []
    value_log = []

    scapi, is_visible = connect_scapi()
    model = load_model(scapi, is_visible, args.xml)

    try:
        session = None
        trans_id = None
        schema_path = Path(args.schema)
        
        schema = _setup_dictionary(scapi, model, schema_path, args, dictionary_log)
        
        session = scapi.Sessions.Add()
        session.Open(model)
        trans_id = session.BeginTransaction()
        
        entities_by_name, counts = _inject_data(session, manifest, value_log)
        updates_applied, verified, altered, skipped = counts
        
        session.CommitTransaction(trans_id)
        
        _save_model(scapi, model, args, is_visible)
        
        persisted = _verify_saved_model(args, manifest, schema, value_log)
        
        _record_results(args, is_visible, started, entities_by_name, counts, persisted, dictionary_log, value_log)

    except Exception as e:
        print(f"An error occurred during injection: {e}")
        print("Canceling all changes to prevent corrupting your file...")
        import traceback
        traceback.print_exc()
        if session and trans_id is not None:
            try:
                session.RollbackTransaction(trans_id)
            except Exception:  # nosec B110
                pass
        sys.exit(1)
    finally:
        if session:
            try:
                session.Close()
            except Exception:  # nosec B110
                pass

def _setup_dictionary(scapi, model, schema_path, args, dictionary_log):
    if not schema_path.exists():
        print(f"Notice: Rules file not found at {schema_path}. Skipping the dictionary setup step.")
        return []

    schema = json.loads(schema_path.read_text())
    session_m1 = scapi.Sessions.Add()
    session_m1.Open(model, 1, 0) 
    trans_m1 = session_m1.BeginTransaction()
    
    try:
        m1_objects = session_m1.ModelObjects
        existing_udps = m1_objects.Collect(m1_objects.Root, "Property_Type")
        existing_names = {}
        for u in existing_udps:
            full_name = u.Properties("Name").Value
            if full_name:
                existing_names[full_name.lower()] = u
        
        created_count = 0
        updated_count = 0
        
        for prop in schema:
            c, u = _apply_dictionary_property(m1_objects, prop, existing_names, args.udp_name_style, dictionary_log)
            created_count += c
            updated_count += u
        
        if created_count > 0 or updated_count > 0:
            session_m1.CommitTransaction(trans_m1)
            print(f"       Dictionary updated ({created_count//2} UDPs applied to both Entities & Attributes = {created_count} total).")
        else:
            session_m1.RollbackTransaction(trans_m1)
            
    except Exception as e:
        print(f"Warning: Failed to create properties in the dictionary: {e}")
        session_m1.RollbackTransaction(trans_m1)
    finally:
        session_m1.Close()
        
    return schema

def _apply_dictionary_property(m1_objects, prop, existing_names, udp_name_style, dictionary_log):
    name = prop.get("udp")
    if not name:
        return 0, 0
        
    is_list = str(prop.get("type", "")).strip().lower() == "list"
    ptype = 6 if is_list else 2
    c_count = 0
    u_count = 0
    
    for owner in ["Entity", "Model"]:
        full_name = name if udp_name_style == "bare" else f"{owner}.Logical.{name}"
        already_exists = full_name.lower() in existing_names
        
        record = {
            "udp": name,
            "owner": owner,
            "full_name": full_name,
            "erwin_type": "List" if is_list else "Text",
            "value_list": ",".join(prop.get("value_list") or []),
            "action": "failed",
            "error": "",
        }
        dictionary_log.append(record)
        
        try:
            if already_exists:
                _update_existing_udp(existing_names[full_name.lower()], ptype, owner, prop)
                record["action"] = "updated"
                u_count += 1
            else:
                _create_new_udp(m1_objects, full_name, ptype, owner, prop)
                record["action"] = "created"
                c_count += 1
        except Exception as exc:
            record["error"] = str(exc)[:300]
            print(f"       Warning: could not define {full_name}: {exc}")
            
    return c_count, u_count

def _update_existing_udp(target_udp, ptype, owner, prop):
    target_udp.Properties("tag_Udp_Data_Type").Value = ptype
    try:
        target_udp.Properties("tag_Udp_Owner_Type").Value = owner
        target_udp.Properties("tag_Is_Logical").Value = True
        target_udp.Properties("tag_Is_Physical").Value = True
        target_udp.Properties("tag_Is_Locally_Defined").Value = True
        target_udp.Properties("tag_Is_Scalar").Value = True
        target_udp.Properties("tag_Is_Prefetch").Value = True
    except Exception:  # nosec B110
        pass
    if ptype == 6 and "value_list" in prop:
        try:
            target_udp.Properties("tag_Udp_Values_List").Value = ",".join(prop["value_list"])
        except Exception:  # nosec B110
            pass

def _create_new_udp(m1_objects, full_name, ptype, owner, prop):
    new_udp = m1_objects.Add("Property_Type")
    new_udp.Properties("Name").Value = full_name
    new_udp.Properties("tag_Udp_Owner_Type").Value = owner
    new_udp.Properties("tag_Udp_Data_Type").Value = ptype
    try:
        new_udp.Properties("tag_Is_Logical").Value = True
        new_udp.Properties("tag_Is_Physical").Value = True
        new_udp.Properties("tag_Is_Locally_Defined").Value = True
        new_udp.Properties("tag_Is_Scalar").Value = True
        new_udp.Properties("tag_Is_Prefetch").Value = True
    except Exception:  # nosec B110
        pass
    if ptype == 6 and "value_list" in prop:
        try:
            new_udp.Properties("tag_Udp_Values_List").Value = ",".join(prop["value_list"])
        except Exception:  # nosec B110
            pass

def _inject_data(session, manifest, value_log):
    model_objects = session.ModelObjects
    entity_collection = model_objects.Collect(model_objects.Root, "Entity")
    entities_by_name = {ent.Name.lower(): ent for ent in entity_collection}

    updates_applied = 0
    skipped = 0
    verified = 0
    altered = 0
    occurrences = {}
    
    for row in manifest:
        up, sk, ve, al = _inject_single_value(row, model_objects, entities_by_name, occurrences, value_log)
        updates_applied += up
        skipped += sk
        verified += ve
        altered += al

    print(f"       Successfully injected {updates_applied} values "
          f"({verified} verified by read-back, {altered} altered by erwin, {skipped} skipped).")
          
    return entities_by_name, (updates_applied, verified, altered, skipped)

def _inject_single_value(row, model_objects, entities_by_name, occurrences, value_log):
    entity_name = row.get("entity_name", "")
    udp_name = row.get("udp", "")
    val = row.get("value", "")
    source_path = row.get("source_path", "")
    
    if not udp_name:
        return 0, 0, 0, 0

    occ_key = (entity_name.lower(), udp_name, source_path)
    occurrence = occurrences.get(occ_key, 0)
    occurrences[occ_key] = occurrence + 1
    
    result = {
        "entity_name": entity_name,
        "pd_object_id": row.get("pd_object_id", ""),
        "udp": udp_name,
        "source_path": source_path,
        "occurrence": occurrence,
        "source_value": str(val),
        "target_value": "",
        "applied_to": "Model Root" if not entity_name else "Entity",
        "property_format": "",
        "status": ST_REJECTED,
        "note": "",
    }
    value_log.append(result)

    target_obj = model_objects.Root if not entity_name else entities_by_name.get(entity_name.lower())
    if not target_obj:
        result["status"] = ST_NO_ENTITY
        result["note"] = "No entity of this name exists in the target erwin model."
        return 0, 1, 0, 0

    return _apply_udp_formats(target_obj, udp_name, val, result)

def _apply_udp_formats(target_obj, udp_name, val, result):
    properties = target_obj.Properties
    formats = [udp_name, f"Udp.{udp_name}", f"Entity.Logical.{udp_name}", f"Entity.Physical.{udp_name}", f"Model.Logical.{udp_name}"]
    
    for fmt in formats:
        try:
            prop = properties(fmt)
            prop.Value = str(val)
            result["property_format"] = fmt
            return _verify_injected_value(prop, val, result)
        except Exception as exc:
            result["note"] = str(exc)[:200]
            
    result["status"] = ST_REJECTED
    result["note"] = "erwin accepted none of the known property name formats for this UDP on this object."
    return 0, 1, 0, 0

def _verify_injected_value(prop, val, result):
    try:
        readback = prop.Value
        readback = "" if readback is None else str(readback)
        result["target_value"] = readback
        if readback == str(val):
            result["status"] = ST_VERIFIED
            result["note"] = ""
            return 1, 0, 1, 0
        else:
            result["status"] = ST_ALTERED
            result["note"] = "erwin stored a different value than the one sent."
            return 1, 0, 0, 1
    except Exception as exc:
        result["status"] = ST_UNVERIFIED
        result["note"] = f"Value written but could not be read back: {str(exc)[:150]}"
        return 1, 0, 0, 0

def _save_model(scapi, model, args, is_visible):
    pu_item = scapi.PersistenceUnits.Item(model.ObjectId)
    
    if args.out_erwin:
        if os.path.exists(args.out_erwin):
            try:
                os.remove(args.out_erwin)
            except PermissionError:
                raise PermissionError(f"Cannot overwrite {args.out_erwin} because it is currently open or locked.")
        pu_item.Save(args.out_erwin)
        
    if args.out_xml:
        if is_visible:
            pu_item.Save(str(Path(args.out_xml).resolve()))
        else:
            # Fix:
            # Dropped the f prefix -- no {placeholder} in this message.
            print("CRITICAL WARNING: Cannot Save As .xml because the erwin UI is hidden! Skipping XML export.")
        
    if not args.out_erwin and not args.out_xml and args.xml:
        pu_item.Save()

def _verify_saved_model(args, manifest, schema, value_log):
    persisted = {"attempted": False, "method": "", "reliable": None,
                 "values_found": 0, "confirmed": 0, "not_persisted": 0,
                 "messages": []}
    saved_model = args.out_erwin or args.out_xml or args.xml
    
    if args.verify and udp_readback is not None and saved_model and schema:
        persisted["attempted"] = True
        try:
            expected = udp_readback.expected_from_manifest(manifest, Path(saved_model).stem)
            back = udp_readback.read_udps(saved_model, schema, "binary", expected)
            persisted["method"] = back.method
            persisted["reliable"] = back.reliable
            persisted["values_found"] = len(back.values)
            persisted["messages"] = list(back.messages)
            
            if back.reliable:
                _match_persisted_values(saved_model, back, value_log, persisted)
            else:
                print("       Post-save verification inconclusive: the saved "
                      "file could not be decoded reliably. Run udp_compare.py "
                      "--method com for an authoritative check.")
        except Exception as exc:
            persisted["messages"].append(str(exc)[:300])
            print(f"       Notice: post-save verification failed: {exc}")
            
    return persisted

def _match_persisted_values(saved_model, back, value_log, persisted):
    root = back.model_root_name or Path(saved_model).stem
    for item in value_log:
        owner = item["entity_name"].strip() or root
        on_disk = back.get(owner, item["udp"])
        item["persisted_value"] = "" if on_disk is None else on_disk
        if not str(item["source_value"]).strip():
            item["persisted"] = "blank source"
        elif on_disk is None:
            item["persisted"] = "no"
            persisted["not_persisted"] += 1
        elif str(on_disk) == str(item["source_value"]):
            item["persisted"] = "yes"
            persisted["confirmed"] += 1
        else:
            item["persisted"] = "altered"
            persisted["not_persisted"] += 1
    print("       Post-save verification: "
          f"{persisted['confirmed']} value(s) confirmed in the "
          f"saved file, {persisted['not_persisted']} not.")

def _record_results(args, is_visible, started, entities_by_name, counts, persisted, dictionary_log, value_log):
    updates_applied, verified, altered, skipped = counts
    if args.results_json:
        results = {
            "model_name": Path(args.xml).stem if args.xml else "",
            "erwin_source": str(args.xml) if args.xml else "",
            "erwin_output": args.out_erwin or args.out_xml or (str(args.xml) if args.xml else ""),
            "session": "Attached to the open erwin UI" if is_visible else "Background erwin process",
            "started": started.strftime("%Y-%m-%d %H:%M:%S"),
            "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "entities_in_erwin": len(entities_by_name),
            "totals": {
                "applied": updates_applied,
                "verified": verified,
                "altered": altered,
                "skipped": skipped,
            },
            "udp_name_style": args.udp_name_style,
            "post_save_verification": persisted,
            "dictionary": dictionary_log,
            "values": value_log,
        }
        try:
            results_path = Path(args.results_json)
            results_path.parent.mkdir(parents=True, exist_ok=True)
            results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        except Exception as exc:
            print(f"Warning: could not write the injection results file: {exc}")

if __name__ == "__main__":
    main()