"""Download the VLM weights with a bounded timeout and retries.

huggingface_hub's default download can hang indefinitely on a dropped connection —
it stalled for 55 minutes mid-file on the first attempt. HF_HUB_DOWNLOAD_TIMEOUT
turns a dead socket into an exception, and the retry loop resumes from the
.incomplete files already on disk instead of starting over.

    python fetch_model.py [model_id]
"""

import os
import sys
import time

os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from huggingface_hub import snapshot_download

MODEL = sys.argv[1] if len(sys.argv) > 1 else "Qwen/Qwen2.5-VL-3B-Instruct"
ATTEMPTS = 40

for attempt in range(1, ATTEMPTS + 1):
    try:
        path = snapshot_download(MODEL, max_workers=4)
        print(f"DONE {path}", flush=True)
        break
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"attempt {attempt}/{ATTEMPTS} failed: {type(e).__name__}: {e}", flush=True)
        if attempt == ATTEMPTS:
            sys.exit("FAILED: giving up")
        time.sleep(min(5 * attempt, 60))
