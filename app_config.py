"""
app_config.py

This service's single RuntimeConfig. Settings are read through `cfg` at the
moment they are needed (admin-panel override -> real env var -> default), so
edits made in the admin panel's Environment tab apply without a redeploy.
See runtime_config.py. MONGODB_URI (real env var) is how it finds them.
"""

from runtime_config import make

# Variables reported to the admin panel so it can show what this service started with.
NAMES = ['MONGODB_URI', 'KAGGLE_USERNAME', 'KAGGLE_KEY', 'KAGGLE_KERNEL_REPO', 'KAGGLE_KERNEL_REF', 'GITHUB_TOKEN', 'KAGGLE_KERNEL_DIR', 'STARTING_TIMEOUT_MINUTES', 'READY_MAX_AGE_HOURS', 'MODEL_TUNNEL_URL', 'SESSION_WEBHOOK_SECRET', 'ALLOWED_ORIGINS', 'KAGGLE_DATASET', 'HF_REPO_ID', 'DATASET_TRIGGER_SECRET', 'HF_REVISION', 'DATASET_TITLE', 'DATASET_EXPECTED_FILE', 'DATASET_KERNEL_SLUG', 'DATASET_PREPARE_TIMEOUT_MINUTES']

cfg = make("kb-builder", NAMES)
