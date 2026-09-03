import logging

# win32com / pythoncom only exist on Windows with pywin32 installed.  They are
# imported lazily (inside start()) so that merely importing this module — which
# app.main and the PDM preprocessing flow do — works on any platform.  start()
# still fails on Windows exactly as before when erwin is not available.
try:
    import win32com.client
    import pythoncom
    _COM_AVAILABLE = True
except ImportError:                                   # non-Windows / no pywin32
    win32com = None
    pythoncom = None
    _COM_AVAILABLE = False

logger = logging.getLogger(__name__)

class ErwinSession:
    """
    Responsible for:
    - Starting erwin Data Modeler
    - Managing the COM session (SCAPI)
    - Opening/closing models
    - Saving models
    - Error handling during the session
    """

    def __init__(self):
        self.app = None
        self.persistence_units = None
        self.is_connected = False

    def start(self):
        """Starts the erwin COM session."""
        if not _COM_AVAILABLE:
            raise RuntimeError(
                "pywin32 is not available — the erwin COM session requires "
                "Windows with erwin Data Modeler installed.")
        try:
            logger.info("Starting erwin SCAPI session...")
            # Initialize COM in this thread
            pythoncom.CoInitialize()
            self.app = win32com.client.Dispatch("erwin9.SCAPI")
            self.persistence_units = self.app.PersistenceUnits
            self.is_connected = True
            logger.info("erwin SCAPI session started successfully.")
        except Exception as e:
            logger.error(f"Failed to start erwin SCAPI session: {e}")
            raise

    def close(self):
        """Closes the erwin COM session."""
        if self.is_connected:
            logger.info("Closing erwin SCAPI session...")
            self.persistence_units = None
            self.app = None
            if pythoncom is not None:
                pythoncom.CoUninitialize()
            self.is_connected = False
            logger.info("erwin SCAPI session closed.")
