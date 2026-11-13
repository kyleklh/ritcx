"""Reads settings from a .env file in this folder, so nobody has to edit the scripts.

Create a file named  .env  next to this one (copy .env.example) with lines like:
    RIT_API_KEY=your_key_from_the_RIT_client
    RIT_API=http://localhost:9999/v1        # optional; only if the API port isn't 9999

.env is in .gitignore, so your key is never pushed to GitHub. Real environment variables
(e.g. set in PowerShell) take priority over the file. No extra package needed.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env_file(path=os.path.join(_HERE, ".env")):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.split("#", 1)[0].strip().strip("'\"")
                os.environ.setdefault(key, value)
    except OSError:
        pass


_load_env_file()

API = os.environ.get("RIT_API", "http://localhost:9999/v1")
API_KEY = os.environ.get("RIT_API_KEY", "Rotman")
HEADERS = {"X-API-Key": API_KEY}
