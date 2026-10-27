"""
Mobile API layer for the Sentinel Home Server.

Single integration point for the rest of the app:

    from mobile_api import start_mobile_api
    start_mobile_api()

This runs its own FastAPI/uvicorn process (see mobile_api/main.py for why
it can't be mounted onto the existing Dash/Flask process) and only reads
from the existing application's database, recording storage, device
status, and audio transport -- it never modifies existing behaviour.
See mobile_api/README.md for the full architecture.
"""

from mobile_api.main import start_mobile_api

__all__ = ["start_mobile_api"]
