"""
batch_run.py
------------
This is the master orchestrator script for the Standalone UDP Extraction & Injection tool.

It performs the following:
1. Scans the 'input_erwin_models' directory for .erwin files.
2. Finds the corresponding SAP model (.cdm, .ldm, or .pdm) in the 'input_sap_models' directory.
3. Phase 1: Calls sow_classify.py and erwin_prepare.py to extract UDPs from the SAP model and save them as JSON.
4. Phase 2: Calls erwin_load.py to inject the extracted JSON data into the .erwin file via the COM API.
5. Saves the final file into the 'output_erwin_models' directory.
6. Phase 3: Calls udp_report.py to build <model_name>_UDP_Migration_Report.xlsx
   from the actual injection results and saves it under the matching model-type folder:
   'output_excel_reports/ldm', 'output_excel_reports/cdm', or 'output_excel_reports/pdm'.
"""

import os
import subprocess  # nosec B404
import sys
from pathlib import Path

def main():
    print('============================================================')
    print('      STANDALONE UDP EXTRACTION & INJECTION TOOL            ')
    print('============================================================')

    base_dir = Path(__file__).parent
    input_erwin_dir = base_dir / 'input_erwin_models'
    input_sap_dir = base_dir / 'input_sap_models'
    output_dir = base_dir / 'output_erwin_models'
    json_dir = base_dir / 'output_jsons'
    reports_dir = base_dir / 'output_excel_reports'

    os.makedirs(json_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    python_executable = sys.executable
    erwin_files = list(input_erwin_dir.glob('*.erwin'))

    if not erwin_files:
        print(f'No .erwin files found in: {input_erwin_dir}')
        sys.exit(0)

    for erwin_file in erwin_files:
        _process_model(erwin_file, input_sap_dir, json_dir, output_dir, reports_dir, base_dir, python_executable)

    print('\nBatch execution complete!')

def _process_model(erwin_file, input_sap_dir, json_dir, output_dir, reports_dir, base_dir, python_executable):
    base_name = erwin_file.stem
    print(f'\n--- Processing: {base_name} ---')

    sap_file = _find_sap_file(base_name, input_sap_dir)
    if not sap_file:
        return

    model_type = sap_file.suffix.lower().lstrip('.')
    if model_type not in {'ldm', 'cdm', 'pdm'}:
        print(f'  -> ERROR: Unsupported SAP model type: {sap_file.suffix}')
        return

    model_reports_dir = reports_dir / model_type
    model_reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = model_reports_dir / f'{base_name}_UDP_Migration_Report.xlsx'

    print(f'  -> Model type: {model_type.upper()}')

    model_json_dir, baseline_dir, out_json = _run_phase1(base_name, sap_file, base_dir, json_dir, python_executable)
    if not out_json:
        return

    manifest_file, schema_file, results_file, out_erwin = _run_phase2(
        base_name, erwin_file, model_json_dir, output_dir, base_dir, python_executable)
        
    _run_phase3(base_name, sap_file, out_json, baseline_dir, out_erwin, 
                model_reports_dir, report_file, manifest_file, schema_file, results_file, 
                base_dir, python_executable)
                
    _run_phase4(base_name, sap_file, erwin_file, out_erwin, model_type, 
                manifest_file, schema_file, model_reports_dir, base_dir, python_executable)

def _find_sap_file(base_name, input_sap_dir):
    ldm_file = input_sap_dir / f'{base_name}.ldm'
    pdm_file = input_sap_dir / f'{base_name}.pdm'
    cdm_file = input_sap_dir / f'{base_name}.cdm'

    if ldm_file.exists():
        return ldm_file
    elif pdm_file.exists():
        return pdm_file
    elif cdm_file.exists():
        return cdm_file

    print(
        f'  -> ERROR: Could not find matching {base_name}.ldm, '
        f'.pdm, or .cdm in the input_sap_models folder!'
    )
    print('     Please paste the original SAP model into input_sap_models.')
    return None

def _run_phase1(base_name, sap_file, base_dir, json_dir, python_executable):
    print('  -> Phase 1: Extracting UDPs from SAP model...')
    baseline_dir = json_dir / 'baseline'
    model_json_dir = json_dir / base_name
    os.makedirs(model_json_dir, exist_ok=True)
    os.makedirs(baseline_dir, exist_ok=True)
    out_json = json_dir / f'classification_{base_name}.json'

    try:
        subprocess.run(  # nosec B603
            [python_executable, str(base_dir / 'sow_classify.py'), str(sap_file), '--out', str(out_json)],
            check=True,
        )
        subprocess.run(  # nosec B603
            [python_executable, str(base_dir / 'erwin_prepare.py'), '--baseline', str(baseline_dir), 
             '--outdir', str(model_json_dir), '--model_name', base_name],
            check=True,
        )
        return model_json_dir, baseline_dir, out_json
    except subprocess.CalledProcessError:
        print('  -> ERROR: UDP Extraction failed!')
        return None, None, None

def _run_phase2(base_name, erwin_file, model_json_dir, output_dir, base_dir, python_executable):
    print('  -> Phase 2: Injecting UDPs into .erwin file via COM...')
    manifest_file = model_json_dir / 'property_manifest.json'
    schema_file = model_json_dir / 'udp_schema.json'
    results_file = model_json_dir / 'injection_results.json'
    out_erwin = output_dir / f'{base_name}.erwin'

    if results_file.exists():
        results_file.unlink()

    try:
        subprocess.run(  # nosec B603
            [python_executable, str(base_dir / 'erwin_load.py'), '--xml', str(erwin_file),
             '--manifest', str(manifest_file), '--schema', str(schema_file),
             '--out_erwin', str(out_erwin), '--results_json', str(results_file)],
            check=True,
        )
        print(f'  -> SUCCESS! Final UDP-injected file saved to: {out_erwin}')
    except subprocess.CalledProcessError:
        print('  -> ERROR: UDP Injection failed!')
        print('     Not skipping this model: the comparison phase reads erwin')
        print('     back off disk, which is exactly what is needed to find out')
        print('     what state the model was actually left in.')

    if not results_file.exists():
        print('  -> ERROR: Injection completed but no fresh results file was created!')
        print('     Continuing to the comparison phase - reading erwin back is the')
        print('     only way to find out what actually landed in the model.')
        
    return manifest_file, schema_file, results_file, out_erwin

def _run_phase3(base_name, sap_file, out_json, baseline_dir, out_erwin, model_reports_dir, report_file, manifest_file, schema_file, results_file, base_dir, python_executable):
    print('  -> Phase 3: Generating Excel migration report...')
    results_args = ['--results', str(results_file)] if results_file.exists() else []
    try:
        subprocess.run(  # nosec B603
            [python_executable, str(base_dir / 'udp_report.py'), '--model_name', base_name,
             '--manifest', str(manifest_file), '--schema', str(schema_file),
             '--classification', str(out_json), '--baseline', str(baseline_dir),
             '--sap_model', str(sap_file), '--erwin_out', str(out_erwin),
             '--outdir', str(model_reports_dir)] + results_args,
            check=True,
        )
        print(f'  -> SUCCESS! Excel report saved to: {report_file}')
    except subprocess.CalledProcessError:
        print('  -> ERROR: Excel report generation failed!')

def _run_phase4(base_name, sap_file, erwin_file, out_erwin, model_type, manifest_file, schema_file, model_reports_dir, base_dir, python_executable):
    print('  -> Phase 4: Comparing SAP UDP values against erwin...')
    erwin_to_read = out_erwin if out_erwin.exists() else erwin_file
    if not out_erwin.exists():
        print('     Note: no injected output model found; reading the input')
        print('     model instead, so every value will report as MISSING.')
    try:
        subprocess.run(  # nosec B603
            [python_executable, str(base_dir / 'udp_compare.py'), '--model_name', base_name,
             '--manifest', str(manifest_file), '--schema', str(schema_file),
             '--erwin', str(erwin_to_read), '--sap_model', str(sap_file),
             '--model_type', model_type, '--method', 'auto', '--outdir', str(model_reports_dir)],
            check=True,
        )
    except subprocess.CalledProcessError:
        print('  -> ERROR: UDP comparison failed!')

if __name__ == '__main__':
    main()
