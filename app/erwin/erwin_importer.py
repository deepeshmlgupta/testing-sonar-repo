import logging
import os
from app.erwin.erwin_session import ErwinSession

logger = logging.getLogger(__name__)

class ErwinImporter:
    """
    Responsible only for:
    - Importing CDM/LDM/PDM using the native MIT Bridge (via SCAPI)
    - Saving as .erwin model
    """

    def __init__(self, session: ErwinSession):
        self.session = session

    def import_model(self, source_path: str, target_erwin_path: str) -> bool:
        """
        Imports a PowerDesigner model and saves it as an erwin model.
        """
        logger.info(f"Importing {source_path} using SCAPI...")
        
        if not self.session.is_connected:
            logger.error("Erwin Session is not connected. Cannot import.")
            return False

        if not os.path.exists(source_path):
            logger.error(f"Source model file not found: {source_path}")
            return False

        try:
            # Check if the user already manually imported and saved the model as both .erwin and .xml
            xml_check_path = target_erwin_path.replace(".erwin", ".xml").replace("\\erwin\\", "\\xml\\").replace("/erwin/", "/xml/")
            
            if os.path.exists(target_erwin_path) and os.path.exists(xml_check_path):
                logger.info(f"Found existing imported model files at {target_erwin_path} and {xml_check_path}. Bypassing MIT Bridge import.")
                return True
                
            # MIT Bridge headless automation is strictly locked by erwin's OEM licensing on this environment.
            # Attempting to run it headlessly via SCAPI or MIMB.bat throws fatal server faults (RPC_E_SERVERFAULT).
            logger.error(f"Cannot import {source_path} headlessly due to strict erwin OEM licensing on the MIT Bridge.")
            logger.warning(f"HYBRID WORKFLOW REQUIRED: Please open erwin Data Modeler UI, manually import '{source_path}', then save it as a standard erwin file at '{target_erwin_path}'. Then do 'File -> Save As' and save it as 'XML Standard File (*.xml)' at '{xml_check_path}'. Then re-run this pipeline.")
            return False
            
        except Exception as e:
            logger.error(f"Erwin Import failed for {source_path}: {e}")
            return False
