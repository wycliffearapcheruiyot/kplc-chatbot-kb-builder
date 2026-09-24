> **Part of the [KPLC Chatbot System](https://github.com/wycliffearapcheruiyot/kplc-chatbot-system).**
> This repo: Builds/verifies the chatbot's knowledge-base data
> Sibling repos: [kplc-chatbot-web](https://github.com/wycliffearapcheruiyot/kplc-chatbot-web), [kplc-chatbot-admin](https://github.com/wycliffearapcheruiyot/kplc-chatbot-admin), [kplc-chatbot-gateway](https://github.com/wycliffearapcheruiyot/kplc-chatbot-gateway), [kplc-chatbot-inference](https://github.com/wycliffearapcheruiyot/kplc-chatbot-inference), [kplc-chatbot-dataset-sync](https://github.com/wycliffearapcheruiyot/kplc-chatbot-dataset-sync), [kplc-chatbot-db-infra](https://github.com/wycliffearapcheruiyot/kplc-chatbot-db-infra)

# Kenya Power Chatbot — Python Backend

FastAPI backend from Section 5 of the deployment guide. Connects to MongoDB
Atlas, does retrieval over the 36 knowledge chunks, manages the Kaggle
session lifecycle, and talks to the model over a Cloudflare Tunnel once the
Kaggle notebook (Section 4, built separately) is running.

**Embeddings run on Kaggle, not here.** This backend has no local embedding
model (no `torch` / `sentence-transformers`) — it calls the Kaggle
notebook's `/embed` endpoint for both chunk embeddings and question
embeddings, and only stores the resulting vectors in Mongo. That means
embedding-dependent things (`/chunks/embed`, and therefore retrieval inside
`/chat`) only work while a Kaggle session is `ready`.

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app — all endpoints |
| `model_client.py` | Calls the model server through the Cloudflare Tunnel |
| `session_manager.py` | Owns the idle/starting/ready session state, triggers Kaggle |
| `dataset_manager.py` | Checks whether the dataset is on Kaggle; if not, runs the Hugging Face -> Kaggle script |
| `dataset_routes.py` | `/dataset/ensure` and `/dataset/status` endpoints |
| `dataset_kernel/fetch_dataset.py` | Runs on Kaggle: downloads from Hugging Face, publishes the Kaggle dataset |
| `system_prompt.md` | Your existing system prompt with the `{{KNOWLEDGE_BASE}}` placeholder |
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for required environment variables |
| `.env` | Your real values (git-ignored, stays local) |

## Endpoints

- `GET /health` — sanity check
- `POST /session/start` — triggers the Kaggle notebook
- `GET /session/status` — current session state
- `POST /session/ready` / `POST /session/ended` — webhooks called by the notebook (require `X-Webhook-Secret` header)
- `POST /chat` — `{"question": "..."}` → `{"answer": "..."}`
- `GET /chunks` — list all knowledge chunks
- `PUT /chunks/{chunk_id}` — `{"text": "..."}`, updates a chunk and clears its embedding
- `POST /chunks/embed` — (re-)embeds chunks missing an embedding via Kaggle; `?force=true` to redo all
- `GET /chat_logs?limit=50` — recent conversations
- `POST /dataset/ensure[?wait=SECONDS]` / `GET /dataset/status` — dataset check, see below (require `X-Webhook-Secret` header)

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Then test:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/chunks
curl http://localhost:8000/session/status
```

`/chat` will return `session_not_active` until a Kaggle session is actually
`ready` — that's expected until Section 4 (the notebook) exists. Chunks will
list with no `embedding` field until you call `POST /chunks/embed` against a
`ready` session, too.

## What still needs real values in `.env`

- `KAGGLE_USERNAME` / `KAGGLE_KEY` — from your Kaggle account API token
- `KAGGLE_KERNEL_REPO` / `KAGGLE_KERNEL_REF` — GitHub repo (`owner/repo`) and branch holding the Kaggle notebook; the backend downloads it on each "Start demo". Add `GITHUB_TOKEN` only if that repo is private. Leave `KAGGLE_KERNEL_DIR` empty on Render.
- `MODEL_TUNNEL_URL` — your Cloudflare Tunnel hostname (not built yet)
- `SESSION_WEBHOOK_SECRET` — pick any random string, and use the *same* one in the notebook later
- `ALLOWED_ORIGINS` — add your Vercel and Netlify URLs once those exist

`MONGODB_URI` is already filled in with your real Atlas connection string.

## Deploying to Render

See Section 6 of the deployment guide: push this repo, create a Web Service
pointing at it, set the start command to run `uvicorn main:app --host 0.0.0.0
--port $PORT`, and add all the `.env.example` variables as Render environment
variables (with real values) instead of shipping a `.env` file.

## Dataset check (called by another service)

The calling service sends `POST /dataset/ensure` with `X-Webhook-Secret: <DATASET_TRIGGER_SECRET>`.

1. Render asks Kaggle whether `KAGGLE_DATASET` exists (and contains `DATASET_EXPECTED_FILE`, if set).
2. **Present** -> `200 {"present": true, "status": "ready"}`.
3. **Missing** -> Render pushes `dataset_kernel/fetch_dataset.py` to Kaggle, which downloads
   `HF_REPO_ID` from Hugging Face and publishes it as the dataset. Response is
   `202 {"present": false, "status": "preparing"}`; with `?wait=90` Render holds the request
   up to that many seconds (max 120) and returns 200 if the dataset lands in time.
4. The caller polls `GET /dataset/status` until it returns `200 {"present": true}`.
   A failed run returns `502` with an `error`; the next `ensure` starts a fresh run.
   Repeated triggers while a run is in progress never start a second run.

Wire it into `main.py` (two lines):

```python
from dataset_routes import build_router
app.include_router(build_router(lambda: db))   # `db` = the Mongo database object main.py already uses
```

One-time Kaggle setup: after the first trigger, open the `hf-to-kaggle-dataset` notebook on
Kaggle -> Add-ons -> Secrets and attach `KAGGLE_KEY` (plus `HF_TOKEN` if the Hugging Face repo is
private), then trigger again. If the calling service is a browser page, add its URL to `ALLOWED_ORIGINS`.
