import logging
import os
from app.erwin.erwin_session import ErwinSession

logger = logging.getLogger(__name__)

class ErwinExporter:
    """
    Responsible only for:
    - Opening an existing .erwin model
    - Saving As -> XML Standard Files
    - Producing the final output .xml
    """

    def __init__(self, session: ErwinSession):
        self.session = session

    def export_to_xml(self, erwin_model_path: str, xml_output_path: str) -> bool:
        """
        Exports an .erwin model to an XML Standard File using SCAPI.
        """
        logger.info(f"Opening existing model {erwin_model_path} for export...")
        
        if not self.session.is_connected:
            logger.error("Erwin Session is not connected. Cannot export.")
            return False

        if not os.path.exists(erwin_model_path):
            logger.error(f"Erwin model file not found: {erwin_model_path}")
            return False

        try:
            # Open the .erwin file in default write mode (RDO=Yes causes Server Faults on XML Export)
            pu = self.session.persistence_units.Add(erwin_model_path)
            
            # Save the file. The .xml extension automatically triggers XML Standard Format export in r10
            pu.Save(xml_output_path)
            
            # Close the model in erwin memory to free resources
            self.session.persistence_units.Remove(pu)
            
            logger.info("Model successfully exported to XML.")
            return True
            
        except Exception as e:
            error_msg = str(e)
            if "read only" in error_msg.lower() or "failed to open a file" in error_msg.lower():
                logger.error(f"Erwin model is locked: {erwin_model_path}")
                logger.warning("Please completely CLOSE the model inside the erwin Data Modeler UI, then run this pipeline again.")
            else:
                logger.error(f"Erwin XML Export failed for {erwin_model_path}: {e}")
            return False
