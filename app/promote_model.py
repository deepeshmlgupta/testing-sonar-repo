import os
import shutil
from pathlib import Path
import openpyxl

def run_promotion_scan():
    print("==================================================")
    print("HUMAN-IN-THE-LOOP PROMOTION ENGINE")
    print("==================================================\n")
    
    # Define paths
    base_dir = Path(__file__).resolve().parent.parent
    manual_review_dir = base_dir / "manual_review_reports"
    preprocessed_dir = base_dir / "erwinmodels" / "2_preprocessed"
    final_dir = base_dir / "erwinmodels" / "3_final"
    
    # Ensure directories exist
    os.makedirs(manual_review_dir, exist_ok=True)
    os.makedirs(final_dir / "erwin", exist_ok=True)
    os.makedirs(final_dir / "xml", exist_ok=True)
    
    # Find all Excel files in the manual review folder (and its subfolders)
    excel_files = list(manual_review_dir.rglob("*.xlsx"))
    
    if not excel_files:
        print(f"No Excel reports found in '{manual_review_dir.name}'.")
        print("Drop a reviewed Excel report in this folder to promote it.")
        return
        
    for excel_path in excel_files:
        raw_stem = excel_path.stem
        import re
        # Remove the fidelity score suffix like "_98.5%_Fidelity" to get the true model name
        base_model_name = re.sub(r'_\d+\.\d+%_Fidelity$', '', raw_stem)
        
        print(f"\nProcessing report: {excel_path.name}")
        print(f"Target model: {base_model_name}")
            
        try:
            wb = openpyxl.load_workbook(excel_path)
        except Exception as e:
            print(f"  [ERROR] Could not read {excel_path.name}: {e}")
            continue
            
        # Check all sheets for "Manual Review" overrides
        all_warnings_acceptable = True
        found_mismatches = False
        
        for sheet_name in wb.sheetnames:
            # The warnings are placed on the 'FINDINGS' tab
            if sheet_name == "FINDINGS":
                ws = wb[sheet_name]
                
                # Find the "Manual Review" and "Severity" columns
                header_row = ws[1]
                review_col_idx = None
                severity_col_idx = None
                for cell in header_row:
                    if cell.value and "Manual Review" in str(cell.value):
                        review_col_idx = cell.column
                    elif cell.value and "Severity" in str(cell.value):
                        severity_col_idx = cell.column
                        
                if review_col_idx is None or severity_col_idx is None:
                    print(f"  [WARNING] Required columns not found in sheet '{sheet_name}'. Skipping.")
                    all_warnings_acceptable = False
                    break
                    
                found_mismatches = True
                
                # Scan all rows in this sheet
                for row_idx in range(2, ws.max_row + 1):
                    # Check if this row actually has a mismatch value
                    mapping_cell = ws.cell(row=row_idx, column=1).value
                    if not mapping_cell:
                        continue # Skip empty rows
                        
                    severity_val = str(ws.cell(row=row_idx, column=severity_col_idx).value).strip().upper()
                    
                    if severity_val == "INFO":
                        continue # Automatically ignore INFO items
                        
                    if severity_val == "CRITICAL":
                        print(f"  -> Found CRITICAL issue on row {row_idx} of '{sheet_name}'. Criticals cannot be overridden. Promotion blocked.")
                        all_warnings_acceptable = False
                        break
                        
                    review_value = ws.cell(row=row_idx, column=review_col_idx).value
                    if not review_value or str(review_value).strip().lower() != "acceptable":
                        print(f"  -> Found unapproved WARNING on row {row_idx} of '{sheet_name}'. Promotion blocked.")
                        all_warnings_acceptable = False
                        break
                        
            if not all_warnings_acceptable:
                break
                
        if not found_mismatches:
            print("  [INFO] No mismatch sheets found to review.")
            continue
            
        if all_warnings_acceptable:
            print(f"  -> All warnings marked 'Acceptable'! Promoting {base_model_name} to 3_final...")
            
            # Source files
            src_erwin = preprocessed_dir / "erwin" / f"{base_model_name}.erwin"
            src_xml = preprocessed_dir / "xml" / f"{base_model_name}.xml"
            
            # Destination files
            dest_erwin = final_dir / "erwin" / f"{base_model_name}.erwin"
            dest_xml = final_dir / "xml" / f"{base_model_name}.xml"
            
            promoted = False
            if src_erwin.exists():
                shutil.copy2(src_erwin, dest_erwin)
                print(f"    Copied {base_model_name}.erwin")
                promoted = True
            
            if src_xml.exists():
                shutil.copy2(src_xml, dest_xml)
                print(f"    Copied {base_model_name}.xml")
                promoted = True
                
            if promoted:
                print("  [SUCCESS] Promotion complete.")
                
                # Update the Excel file's internal score to 100%
                if "SUMMARY" in wb.sheetnames:
                    ws_summary = wb["SUMMARY"]
                    for cell in ws_summary[2]:
                        if cell.value == "Fidelity %":
                            # The data starts on row 3
                            for r in range(3, ws_summary.max_row + 1):
                                if ws_summary.cell(row=r, column=cell.column).value is not None:
                                    ws_summary.cell(row=r, column=cell.column).value = "100.0 (Approved)"
                            break
                            
                # Save and rename the file to reflect 100% Fidelity
                archive_dir = manual_review_dir / "archived_promotions"
                archive_dir.mkdir(exist_ok=True)
                
                new_filename = f"{base_model_name}_100.0%_Fidelity_Approved.xlsx"
                new_filepath = archive_dir / new_filename
                
                # Save the modified workbook directly to the archive folder
                wb.save(new_filepath)
                wb.close()
                
                # Delete the original file from the pickup folder
                excel_path.unlink()
                
                print(f"  -> Updated Excel score to 100% and moved to archive: {new_filename}")
            else:
                print(f"  [ERROR] Source files missing in {preprocessed_dir}. Could not promote.")

if __name__ == "__main__":
    run_promotion_scan()
