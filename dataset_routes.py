"""
dataset_routes.py

Endpoints another service calls to ask "is the dataset in Kaggle?".
Both require the header  X-Webhook-Secret: <DATASET_TRIGGER_SECRET>.

  POST /dataset/ensure[?wait=SECONDS]
      Present on Kaggle            -> 200 {"present": true,  "status": "ready"}
      Missing: starts the Hugging Face -> Kaggle run, then
        finished within `wait`     -> 200 {"present": true,  "status": "ready"}
        still running              -> 202 {"present": false, "status": "preparing"}
        run failed                 -> 502 {"present": false, "status": "failed", "error": ...}
      `wait` defaults to 0 and is capped at MAX_WAIT_SECONDS.

  GET /dataset/status
      Same answers, but never starts a run. Poll this after a 202.

Wire it up in main.py (2 lines):
    from dataset_routes import build_router
    app.include_router(build_router(lambda: db))     # `db` = your Mongo database object
"""

import hmac
import os
import time

from fastapi import APIRouter, Header, HTTPException, Response

from dataset_manager import DatasetError, check_dataset

MAX_WAIT_SECONDS = 120
POLL_EVERY_SECONDS = 5


def build_router(get_db) -> APIRouter:
    router = APIRouter(prefix="/dataset", tags=["dataset"])

    def _authorize(secret):
        expected = os.environ.get("DATASET_TRIGGER_SECRET")
        if not expected:
            raise HTTPException(500, "DATASET_TRIGGER_SECRET is not configured on the server.")
        if not secret or not hmac.compare_digest(secret, expected):
            raise HTTPException(401, "Invalid or missing X-Webhook-Secret.")

    def _respond(result: dict, response: Response) -> dict:
        response.status_code = {"ready": 200, "preparing": 202, "failed": 502}.get(result["status"], 200)
        return result

    @router.post("/ensure")
    def ensure(response: Response, wait: int = 0, x_webhook_secret: str = Header(None)):
        _authorize(x_webhook_secret)
        try:
            result = check_dataset(get_db(), start_if_missing=True)
            deadline = time.monotonic() + max(0, min(wait, MAX_WAIT_SECONDS))
            while result["status"] == "preparing" and time.monotonic() < deadline:
                time.sleep(POLL_EVERY_SECONDS)
                result = check_dataset(get_db(), start_if_missing=False)
        except DatasetError as e:
            raise HTTPException(500, str(e))
        return _respond(result, response)

    @router.get("/status")
    def status(response: Response, x_webhook_secret: str = Header(None)):
        _authorize(x_webhook_secret)
        try:
            result = check_dataset(get_db(), start_if_missing=False)
        except DatasetError as e:
            raise HTTPException(500, str(e))
        return _respond(result, response)

    return router
