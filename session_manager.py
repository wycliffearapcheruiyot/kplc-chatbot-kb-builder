"""
session_manager.py

Owns the idle -> starting -> ready -> idle lifecycle described in Section 1
of the deployment guide. State lives in a single document in the
`session_state` collection: {"_id": "session", "status": ..., ...timestamps}

start_session() does NOT run the model. It only *starts* a run on Kaggle:
  1. downloads the notebook repo (serve.py + kernel-metadata.json) from GitHub
     -- KAGGLE_KERNEL_REPO, at KAGGLE_KERNEL_REF -- into a temp folder
  2. runs `kaggle kernels push` on it, which starts the run on a Kaggle GPU
The model server then runs entirely on Kaggle and calls the /session/ready
and /session/ended webhooks. Nothing heavy ever runs on Render.

For local development you can skip GitHub by setting KAGGLE_KERNEL_DIR to a
local folder instead.

Safety nets (a Kaggle run can die without calling any webhook):
  - `starting` for longer than STARTING_TIMEOUT_MINUTES  -> treated as idle
  - `ready`    for longer than READY_MAX_AGE_HOURS       -> treated as idle
"""

import io
import os
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import requests

SESSION_DOC_ID = "session"
VALID_STATUSES = {"idle", "starting", "ready"}

STARTING_TIMEOUT = timedelta(minutes=int(os.environ.get("STARTING_TIMEOUT_MINUTES", "15")))
READY_MAX_AGE = timedelta(hours=int(os.environ.get("READY_MAX_AGE_HOURS", "12")))


class SessionError(Exception):
    """Raised when a session action can't be completed."""


def _now():
    return datetime.now(timezone.utc)


def _session_collection(db):
    return db["session_state"]


def _as_utc(dt):
    """pymongo returns naive datetimes by default; ours are always UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_status(db) -> dict:
    col = _session_collection(db)
    doc = col.find_one({"_id": SESSION_DOC_ID})
    if not doc:
        doc = {"_id": SESSION_DOC_ID, "status": "idle", "updated_at": _now()}
        col.insert_one(doc)
        return doc

    # Recover from Kaggle runs that died without calling /session/ended
    # (crash, out of memory, quota exhausted, 9-12h session cap).
    status, updated = doc.get("status"), _as_utc(doc.get("updated_at"))
    if updated:
        age = _now() - updated
        stale = (status == "starting" and age > STARTING_TIMEOUT) or (
            status == "ready" and age > READY_MAX_AGE
        )
        if stale:
            # Filter on the old status so a concurrent webhook can't be clobbered.
            col.update_one(
                {"_id": SESSION_DOC_ID, "status": status},
                {"$set": {"status": "idle", "updated_at": _now(),
                          "note": f"auto-reset: stuck in '{status}'"}},
            )
            doc = col.find_one({"_id": SESSION_DOC_ID})
    return doc


def _download_kernel_from_github(dest: Path) -> None:
    """Downloads the notebook repo as a tarball (no git binary needed) and
    unpacks it flat into `dest`, so kernel-metadata.json sits at its top level."""
    repo = os.environ.get("KAGGLE_KERNEL_REPO")  # "owner/repo"
    ref = os.environ.get("KAGGLE_KERNEL_REF", "main")
    if not repo:
        raise SessionError(
            "KAGGLE_KERNEL_REPO is not set. Set it to the GitHub repo holding the "
            "Kaggle notebook, e.g. 'your-username/kplc-kaggle-notebook' "
            "(or set KAGGLE_KERNEL_DIR to a local folder for development)."
        )
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")  # only needed if the repo is private
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/tarball/{ref}",
            headers=headers, timeout=30,
        )
    except requests.exceptions.RequestException as e:
        raise SessionError(f"Could not reach GitHub to fetch {repo}: {e}") from e
    if resp.status_code != 200:
        hint = " (private repo? set GITHUB_TOKEN)" if resp.status_code in (401, 403, 404) else ""
        raise SessionError(f"GitHub returned {resp.status_code} for {repo}@{ref}{hint}.")

    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = PurePosixPath(member.name).parts[1:]  # drop GitHub's "owner-repo-sha/" prefix
            if not parts or ".." in parts or PurePosixPath(member.name).is_absolute():
                continue
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())

    if not (dest / "kernel-metadata.json").exists():
        raise SessionError(f"{repo}@{ref} has no kernel-metadata.json at its top level.")


def _kaggle_push(kernel_dir: str, env: dict) -> None:
    try:
        result = subprocess.run(
            ["kaggle", "kernels", "push", "-p", kernel_dir],
            capture_output=True, text=True, timeout=60, env=env, check=False,
        )
    except FileNotFoundError as e:
        raise SessionError(
            "The `kaggle` CLI isn't installed/on PATH. It comes from the `kaggle` "
            "package in requirements.txt."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise SessionError("Timed out calling `kaggle kernels push`.") from e
    if result.returncode != 0:
        raise SessionError(f"`kaggle kernels push` failed: {result.stderr.strip() or result.stdout.strip()}")


def start_session(db) -> dict:
    """Triggers the Kaggle notebook, unless a session is already starting/ready
    (so two recruiters clicking at once don't trigger two Kaggle runs)."""
    current = get_status(db)

    if current["status"] in ("starting", "ready"):
        return current  # no-op, just report current state

    kaggle_username = os.environ.get("KAGGLE_USERNAME")
    kaggle_key = os.environ.get("KAGGLE_KEY")
    if not (kaggle_username and kaggle_key):
        raise SessionError("KAGGLE_USERNAME / KAGGLE_KEY are not set in the environment.")
    env = {**os.environ, "KAGGLE_USERNAME": kaggle_username, "KAGGLE_KEY": kaggle_key}

    local_dir = os.environ.get("KAGGLE_KERNEL_DIR")
    if local_dir:  # local development override
        _kaggle_push(local_dir, env)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            _download_kernel_from_github(Path(tmp))
            _kaggle_push(tmp, env)

    _session_collection(db).update_one(
        {"_id": SESSION_DOC_ID},
        {"$set": {"status": "starting", "updated_at": _now()}},
        upsert=True,
    )
    return get_status(db)


def mark_ready(db) -> dict:
    """Called by the /session/ready webhook once the notebook confirms its
    FastAPI server and Cloudflare Tunnel are both live."""
    _session_collection(db).update_one(
        {"_id": SESSION_DOC_ID},
        {"$set": {"status": "ready", "updated_at": _now()}},
        upsert=True,
    )
    return get_status(db)


def mark_ended(db) -> dict:
    """Called by the /session/ended webhook when the notebook's idle-watchdog
    shuts things down (or the session otherwise ends)."""
    _session_collection(db).update_one(
        {"_id": SESSION_DOC_ID},
        {"$set": {"status": "idle", "updated_at": _now()}},
        upsert=True,
    )
    return get_status(db)
