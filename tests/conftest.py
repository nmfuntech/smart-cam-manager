"""Hermetic test environment.

The suite must behave the same on a dev Mac, on CI and inside the mini PC
deploy worktree. The real ``.env`` of a machine (Telegram bot enabled, agent,
automation...) must never leak into a test run, so it is not loaded at all.
"""

import os

os.environ["BLACKFRAME_SKIP_DOTENV"] = "1"
