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
            print(f"CRITICAL ERROR: You are trying to load an XML file, but the erwin UI is not open on your desktop!")
            print(f"Due to an erwin bug, loading XML files in the background causes a catastrophic crash.")
            print(f"Please double-click the erwin application to open it on your screen, then run the pipeline again.")
            sys.exit(1)
            
        print(f"       Connecting to erwin...")
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
    # Setup the command line options you can type
    ap = argparse.ArgumentParser(description="Inject custom properties into an erwin model.")
    ap.add_argument("--xml", help="Path to the erwin XML model to load. If left blank, uses the active open model.")
    ap.add_argument("--manifest", default="erwin_input/property_manifest.json", help="Path to the data file (property_manifest.json)")
    ap.add_argument("--schema", default="erwin_input/udp_schema.json", help="Path to the rules file (udp_schema.json)")
    ap.add_argument("--out_erwin", help="Optional path to Save As an .erwin file.")
    ap.add_argument("--out_xml", help="Optional path to Save As an .xml file.")
    ap.add_argument("--results_json", help="Optional path to write the injection results, "
                                          "used to build the Excel migration report.")
    ap.add_argument("--udp_name_style", default="qualified",
                    choices=["qualified", "bare"],
                    help="How UDP definitions are named in the erwin dictionary. "
                         "'qualified' uses <Owner>.Logical.<name> (current behaviour, "
                         "proven to store values correctly). 'bare' uses <name> and "
                         "relies on tag_Udp_Owner_Type alone - try this if UDPs store "
                         "values but do not appear where expected in the erwin UI.")
    ap.add_argument("--no_verify", dest="verify", action="store_false",
                    help="Skip re-opening the saved model to verify that UDP values "
                         "actually persisted to disk.")
    args = ap.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"Error: Data file {manifest_path} was not found.")
        sys.exit(1)

    # ---------------------------------------------------------------------
    # STEP 1: LOAD DATA
    # ---------------------------------------------------------------------
    # print(f"Loading data from {manifest_path}...")
    manifest = json.loads(manifest_path.read_text())

    # Records of what erwin actually did, consumed by udp_report.py.
    started = datetime.now()
    dictionary_log = []
    value_log = []

    # ---------------------------------------------------------------------
    # STEP 2: CONNECT TO ERWIN
    # ---------------------------------------------------------------------
    # Connect to erwin and load the file
    scapi, is_visible = connect_scapi()
    model = load_model(scapi, is_visible, args.xml)

    try:
        session = None
        schema_path = Path(args.schema)
        
        # ---------------------------------------------------------------------
        # STEP 3: SETUP THE DICTIONARY (SO PROPERTIES SHOW IN UI)
        # ---------------------------------------------------------------------
        if schema_path.exists():
            # print(f"Loading property definitions from {schema_path}...")
            schema = json.loads(schema_path.read_text())
            
            # print("Opening the erwin dictionary to update property definitions...")
            session_m1 = scapi.Sessions.Add()
            # The '1' below tells erwin we are editing the Dictionary/Metadata, not the actual data
            session_m1.Open(model, 1, 0) 
            trans_m1 = session_m1.BeginTransaction()
            
            try:
                m1_objects = session_m1.ModelObjects
                # Gather all existing custom properties so we don't duplicate them
                existing_udps = m1_objects.Collect(m1_objects.Root, "Property_Type")
                existing_names = {}
                for u in existing_udps:
                    full_name = u.Properties("Name").Value
                    if full_name:
                        existing_names[full_name.lower()] = u
                
                created_count = 0
                updated_count = 0
                
                # Loop through each property rule from the JSON file
                for prop in schema:
                    name = prop.get("udp")
                    if not name:
                        continue
                        
                    # Check if this property should be a Drop-Down List (6) or plain Text (2)
                    is_list = str(prop.get("type", "")).strip().lower() == "list"
                    ptype = 6 if is_list else 2
                    
                    # Create the property for both Entities and the overall Model
                    for owner in ["Entity", "Model"]:
                        # The dictionary entry name. "qualified" embeds the owner in
                        # the name as well as setting tag_Udp_Owner_Type; that is how
                        # this tool has always worked and it does store values
                        # correctly. "bare" leaves the owner to the tag alone.
                        if args.udp_name_style == "bare":
                            full_name = name
                        else:
                            full_name = f"{owner}.Logical.{name}"
                        already_exists = full_name.lower() in existing_names
                        
                        # Record every attempt so the Excel report can show which
                        # definitions erwin really accepted.
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
                                # The property already exists, let's update it
                                target_udp = existing_names[full_name.lower()]
                                target_udp.Properties("tag_Udp_Data_Type").Value = ptype
                                try:
                                    # Ensure all the hidden switches are turned on so it is visible in the UI
                                    target_udp.Properties("tag_Udp_Owner_Type").Value = owner
                                    target_udp.Properties("tag_Is_Logical").Value = True
                                    target_udp.Properties("tag_Is_Physical").Value = True
                                    target_udp.Properties("tag_Is_Locally_Defined").Value = True # This means "User-Defined"
                                    target_udp.Properties("tag_Is_Scalar").Value = True
                                    target_udp.Properties("tag_Is_Prefetch").Value = True
                                except Exception:  # nosec B110
                                    pass
                                
                                # If it's a Drop-Down list, add the comma-separated options
                                if ptype == 6 and "value_list" in prop:
                                    try:
                                        list_str = ",".join(prop["value_list"])
                                        target_udp.Properties("tag_Udp_Values_List").Value = list_str
                                    except Exception:  # nosec B110
                                        pass
                                record["action"] = "updated"
                                updated_count += 1
                            else:
                                # The property does not exist, let's create a brand new one
                                new_udp = m1_objects.Add("Property_Type")
                                new_udp.Properties("Name").Value = full_name
                                new_udp.Properties("tag_Udp_Owner_Type").Value = owner
                                new_udp.Properties("tag_Udp_Data_Type").Value = ptype
                                try:
                                    # Turn on all the UI visibility switches
                                    new_udp.Properties("tag_Is_Logical").Value = True
                                    new_udp.Properties("tag_Is_Physical").Value = True
                                    new_udp.Properties("tag_Is_Locally_Defined").Value = True
                                    new_udp.Properties("tag_Is_Scalar").Value = True
                                    new_udp.Properties("tag_Is_Prefetch").Value = True
                                except Exception:  # nosec B110
                                    pass
                                    
                                # If it's a Drop-Down list, add the comma-separated options
                                if ptype == 6 and "value_list" in prop:
                                    try:
                                        list_str = ",".join(prop["value_list"])
                                        new_udp.Properties("tag_Udp_Values_List").Value = list_str
                                    except Exception:  # nosec B110
                                        pass
                                record["action"] = "created"
                                created_count += 1
                        except Exception as exc:
                            # One rejected definition should not abandon the rest.
                            record["error"] = str(exc)[:300]
                            print(f"       Warning: could not define {full_name}: {exc}")
                
                # Save all the dictionary changes we just made
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
        else:
            print(f"Notice: Rules file not found at {schema_path}. Skipping the dictionary setup step.")
            schema = []

        # ---------------------------------------------------------------------
        # STEP 4: INJECT DATA INTO THE MODEL
        # ---------------------------------------------------------------------
        # print("Opening session to inject data...")
        session = scapi.Sessions.Add()
        session.Open(model)
        
        # Start a single transaction so erwin doesn't slow down saving every single row
        trans_id = session.BeginTransaction()
        
        model_objects = session.ModelObjects
        # Get all the tables (Entities) in the file at once
        entity_collection = model_objects.Collect(model_objects.Root, "Entity")
        
        # Save them in a fast dictionary (hashmap) so we can look them up instantly by name
        entities_by_name = {}
        for ent in entity_collection:
            entities_by_name[ent.Name.lower()] = ent

        updates_applied = 0
        skipped = 0
        verified = 0
        altered = 0
        
        # Occurrence counter keeps duplicate (entity, udp, source_path) rows
        # distinct, so the report can join each result back to its source row.
        occurrences = {}
        
        # print("Injecting values...")
        for row in manifest:
            entity_name = row.get("entity_name", "")
            udp_name = row.get("udp", "")
            val = row.get("value", "")
            source_path = row.get("source_path", "")
            
            if not udp_name:
                continue

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

            # Figure out if we are updating the overall Model, or a specific Table (Entity)
            if not entity_name:
                target_obj = model_objects.Root
            else:
                target_obj = entities_by_name.get(entity_name.lower())

            if not target_obj:
                # The table doesn't exist in the file - record it, don't guess
                result["status"] = ST_NO_ENTITY
                result["note"] = "No entity of this name exists in the target erwin model."
                skipped += 1
                continue

            properties = target_obj.Properties
            success = False
            
            # erwin's internal engine is very picky about naming formats. We try multiple until one sticks.
            formats = [udp_name, f"Udp.{udp_name}", f"Entity.Logical.{udp_name}", f"Entity.Physical.{udp_name}", f"Model.Logical.{udp_name}"]
            
            for fmt in formats:
                try:
                    prop = properties(fmt)
                    prop.Value = str(val)
                    success = True
                    result["property_format"] = fmt
                    
                    # Read the value straight back out of erwin. This is the
                    # only way to know erwin kept what we sent - a List UDP
                    # silently drops a value outside its permitted list.
                    try:
                        readback = prop.Value
                        readback = "" if readback is None else str(readback)
                        result["target_value"] = readback
                        if readback == str(val):
                            result["status"] = ST_VERIFIED
                            # Clear any error left by an earlier format attempt.
                            result["note"] = ""
                            verified += 1
                        else:
                            result["status"] = ST_ALTERED
                            result["note"] = "erwin stored a different value than the one sent."
                            altered += 1
                    except Exception as exc:
                        result["status"] = ST_UNVERIFIED
                        result["note"] = f"Value written but could not be read back: {str(exc)[:150]}"
                    break # Stop trying formats once we succeed
                except Exception as exc:
                    result["note"] = str(exc)[:200]
            
            if not success:
                # erwin rejected the property (e.g. trying to add a table property to a model)
                result["status"] = ST_REJECTED
                result["note"] = ("erwin accepted none of the known property name formats "
                                  "for this UDP on this object.")
                skipped += 1
            else:
                updates_applied += 1

        print(f"       Successfully injected {updates_applied} values "
              f"({verified} verified by read-back, {altered} altered by erwin, {skipped} skipped).")
        
        # ---------------------------------------------------------------------
        # STEP 5: SAVE & CLOSE
        # ---------------------------------------------------------------------
        # print("Saving all changes...")
        session.CommitTransaction(trans_id)
        
        pu_item = scapi.PersistenceUnits.Item(model.ObjectId)
        
        if args.out_erwin:
            # print(f"Saving As .erwin: {args.out_erwin}")
            if os.path.exists(args.out_erwin):
                try:
                    os.remove(args.out_erwin)
                except PermissionError:
                    raise PermissionError(f"Cannot overwrite {args.out_erwin} because it is currently open or locked. Please close it in erwin Data Modeler and try again!")
            pu_item.Save(args.out_erwin)
            
        if args.out_xml:
            if is_visible:
                # print(f"Saving As .xml: {args.out_xml}")
                pu_item.Save(str(Path(args.out_xml).resolve()))
            else:
                print(f"CRITICAL WARNING: Cannot Save As .xml because the erwin UI is hidden! Skipping XML export to prevent crashing!")
            
        if not args.out_erwin and not args.out_xml and args.xml:
            # Overwrite the original file with all our new data
            pu_item.Save()
            # print("File overwritten successfully.")
            
        # ---------------------------------------------------------------------
        # STEP 6: RECORD THE RESULTS (input for the Excel migration report)
        # ---------------------------------------------------------------------
        # ---------------------------------------------------------------------
        # STEP 5b: VERIFY THE SAVED FILE
        # The read-back inside the injection loop proves the session accepted a
        # value. It cannot prove the value survived being written to disk,
        # because at that point nothing had been. Re-reading the saved model
        # closes that gap - it is what turns "injected" into "verified".
        # ---------------------------------------------------------------------
        persisted = {"attempted": False, "method": "", "reliable": None,
                     "values_found": 0, "confirmed": 0, "not_persisted": 0,
                     "messages": []}
        saved_model = args.out_erwin or args.out_xml or args.xml
        if args.verify and udp_readback is not None and saved_model and schema:
            persisted["attempted"] = True
            try:
                expected = udp_readback.expected_from_manifest(
                    manifest, Path(saved_model).stem)
                back = udp_readback.read_udps(saved_model, schema, "binary", expected)
                persisted["method"] = back.method
                persisted["reliable"] = back.reliable
                persisted["values_found"] = len(back.values)
                persisted["messages"] = list(back.messages)
                if back.reliable:
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
                else:
                    print("       Post-save verification inconclusive: the saved "
                          "file could not be decoded reliably. Run udp_compare.py "
                          "--method com for an authoritative check.")
            except Exception as exc:
                persisted["messages"].append(str(exc)[:300])
                print(f"       Notice: post-save verification failed: {exc}")

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
            
        # print("Finished.")
    except Exception as e:
        print(f"An error occurred during injection: {e}")
        print("Canceling all changes to prevent corrupting your file...")
        import traceback
        traceback.print_exc()
        if session:
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
        # print("Finished.")

if __name__ == "__main__":
    main()
