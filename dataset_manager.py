"""
dataset_manager.py

Answers "is the dataset in Kaggle?" and, if it isn't, gets it there:

  1. `kaggle datasets status <owner/slug>`  -> is the dataset already on Kaggle?
        yes -> {"present": True}
  2. no -> pushes dataset_kernel/fetch_dataset.py to Kaggle as a script run.
        That script downloads the repo from Hugging Face and publishes it as
        the Kaggle dataset. Nothing heavy runs on Render.
  3. every later check re-asks Kaggle; once the dataset exists (and, if
        DATASET_EXPECTED_FILE is set, contains that file) -> {"present": True}

State lives in one Mongo document ({"_id": "dataset"}) in `dataset_state`, so
two triggers arriving together only start ONE Kaggle run.

Environment variables
  KAGGLE_USERNAME / KAGGLE_KEY   already used by session_manager.py
  KAGGLE_DATASET                 "owner/slug" of the dataset that must exist
  HF_REPO_ID                     Hugging Face repo to copy into it, e.g. "org/model"
  HF_REVISION                    optional branch/tag/commit
  DATASET_TITLE                  optional title for the Kaggle dataset
  DATASET_EXPECTED_FILE          optional file name that must be in the dataset
  DATASET_KERNEL_SLUG            optional, default "hf-to-kaggle-dataset"
  DATASET_PREPARE_TIMEOUT_MINUTES optional, default 90
"""

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pymongo.errors import DuplicateKeyError

DOC_ID = "dataset"
KERNEL_TEMPLATE = Path(__file__).parent / "dataset_kernel" / "fetch_dataset.py"
KERNEL_FAILED = {"error", "cancelacknowledged", "cancelrequested"}


class DatasetError(Exception):
    """Raised when the dataset check/trigger can't be completed."""


def _now():
    return datetime.now(timezone.utc)


def _as_utc(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _col(db):
    return db["dataset_state"]


def _timeout():
    return timedelta(minutes=int(os.environ.get("DATASET_PREPARE_TIMEOUT_MINUTES", "90")))


def _config():
    dataset = os.environ.get("KAGGLE_DATASET", "").strip()
    hf_repo = os.environ.get("HF_REPO_ID", "").strip()
    if "/" not in dataset:
        raise DatasetError("KAGGLE_DATASET must be set to 'owner/slug'.")
    if not hf_repo:
        raise DatasetError("HF_REPO_ID is not set.")
    user, key = os.environ.get("KAGGLE_USERNAME"), os.environ.get("KAGGLE_KEY")
    if not (user and key):
        raise DatasetError("KAGGLE_USERNAME / KAGGLE_KEY are not set in the environment.")
    return {
        "dataset": dataset,
        "hf_repo": hf_repo,
        "hf_revision": os.environ.get("HF_REVISION", "").strip(),
        "title": os.environ.get("DATASET_TITLE", "").strip() or dataset.split("/", 1)[1],
        "expected_file": os.environ.get("DATASET_EXPECTED_FILE", "").strip(),
        "kernel_slug": os.environ.get("DATASET_KERNEL_SLUG", "hf-to-kaggle-dataset").strip(),
        "env": {**os.environ, "KAGGLE_USERNAME": user, "KAGGLE_KEY": key},
        "username": user,
    }


def _kaggle(args, env, timeout=60):
    try:
        return subprocess.run(
            ["kaggle", *args], capture_output=True, text=True,
            timeout=timeout, env=env, check=False,
        )
    except FileNotFoundError as e:
        raise DatasetError("The `kaggle` CLI isn't installed/on PATH (add `kaggle` to requirements.txt).") from e
    except subprocess.TimeoutExpired as e:
        raise DatasetError(f"Timed out calling `kaggle {' '.join(args[:2])}`.") from e


def dataset_present(cfg) -> bool:
    """True only if Kaggle reports the dataset as ready (and has the expected file)."""
    res = _kaggle(["datasets", "status", cfg["dataset"]], cfg["env"])
    if res.returncode != 0 or res.stdout.strip().lower() != "ready":
        return False  # missing, still processing, or not visible to this account
    if cfg["expected_file"]:
        files = _kaggle(["datasets", "files", cfg["dataset"]], cfg["env"])
        if files.returncode != 0 or cfg["expected_file"] not in files.stdout:
            return False
    return True


def _kernel_status(cfg) -> str:
    """Lower-cased Kaggle run status: queued / running / complete / error / ..."""
    res = _kaggle(["kernels", "status", f"{cfg['username']}/{cfg['kernel_slug']}"], cfg["env"])
    if res.returncode != 0:
        return "unknown"
    m = re.search(r'status\s+"?(?:KernelWorkerStatus\.)?([A-Za-z_]+)"?', res.stdout)
    return re.sub(r"[^a-z]", "", m.group(1).lower()) if m else "unknown"


def _push_kernel(cfg) -> None:
    script = KERNEL_TEMPLATE.read_text()
    public_cfg = {k: cfg[k] for k in ("dataset", "hf_repo", "hf_revision", "title")}
    script = script.replace("__CONFIG__", repr(public_cfg))
    metadata = {
        "id": f"{cfg['username']}/{cfg['kernel_slug']}",
        "title": cfg["kernel_slug"],
        "code_file": "fetch_dataset.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": "true",
        "enable_gpu": "false",
        "enable_internet": "true",
        "dataset_sources": [],
        "competition_sources": [],
        "kernel_sources": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "fetch_dataset.py").write_text(script)
        (Path(tmp) / "kernel-metadata.json").write_text(json.dumps(metadata))
        res = _kaggle(["kernels", "push", "-p", tmp], cfg["env"])
    if res.returncode != 0:
        raise DatasetError(f"`kaggle kernels push` failed: {res.stderr.strip() or res.stdout.strip()}")


def _set(db, **fields):
    _col(db).update_one({"_id": DOC_ID}, {"$set": {**fields, "updated_at": _now()}}, upsert=True)


def check_dataset(db, start_if_missing: bool = True) -> dict:
    """
    Returns {"present": bool, "status": "ready" | "preparing" | "failed" | "missing", ...}
      ready     dataset is on Kaggle
      preparing the Hugging Face -> Kaggle run is in progress
      failed    the run errored or timed out (error says why; a new trigger retries)
      missing   only when start_if_missing=False and nothing is running
    """
    cfg = _config()

    if dataset_present(cfg):
        _set(db, status="ready", error=None)
        return {"present": True, "status": "ready"}

    col = _col(db)
    doc = col.find_one({"_id": DOC_ID}) or {}

    if doc.get("status") == "preparing":
        started = _as_utc(doc.get("started_at"))
        ks = _kernel_status(cfg)
        if ks in KERNEL_FAILED:
            _set(db, status="failed", error=f"Kaggle run ended with status '{ks}'. Check the notebook log on Kaggle.")
        elif started and _now() - started > _timeout():
            _set(db, status="failed", error="Timed out waiting for the Hugging Face -> Kaggle run.")
        else:
            # queued / running / complete-but-dataset-still-processing
            return {"present": False, "status": "preparing", "kaggle_run": ks}
        doc = col.find_one({"_id": DOC_ID})

    if doc.get("status") == "failed" and not start_if_missing:
        return {"present": False, "status": "failed", "error": doc.get("error")}
    if not start_if_missing:
        return {"present": False, "status": "missing"}

    # Claim the job atomically so simultaneous triggers start only one run.
    try:
        col.find_one_and_update(
            {"_id": DOC_ID, "status": {"$ne": "preparing"}},
            {"$set": {"status": "preparing", "started_at": _now(), "updated_at": _now(), "error": None}},
            upsert=True,
        )
    except DuplicateKeyError:
        return {"present": False, "status": "preparing"}  # another trigger just claimed it

    try:
        _push_kernel(cfg)
    except DatasetError as e:
        _set(db, status="failed", error=str(e))
        return {"present": False, "status": "failed", "error": str(e)}
    return {"present": False, "status": "preparing"}
