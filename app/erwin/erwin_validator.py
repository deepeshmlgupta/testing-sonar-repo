import logging
from app.erwin.erwin_session import ErwinSession

logger = logging.getLogger(__name__)

class ErwinValidator:
    """
    Responsible for validating erwin models.
    """

    def __init__(self, session: ErwinSession):
        self.session = session

    def validate_model(self, erwin_model_path: str) -> bool:
        """
        Placeholder for erwin model validation logic.
        """
        logger.info(f"Validating erwin model at {erwin_model_path} - Not Implemented Yet")
        return True
