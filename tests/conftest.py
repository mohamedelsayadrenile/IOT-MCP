import os

# Set before any src import so Settings never depends on a real src/.env.
os.environ.setdefault("RENILE_API_TOKEN", "test-token")
