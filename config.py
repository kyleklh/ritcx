"""Reads settings from a .env file in this folder, so nobody has to edit the scripts.

Create a file named  .env  next to this one (copy .env.example) with lines like:
    RIT_API_KEY=your_key_from_the_RIT_client
    RIT_API=http://localhost:9999/v1        # optional; only if the API port isn't 9999

.env is in .gitignore, so your key is never pushed to GitHub. Real environment variables
(e.g. set in PowerShell) take priority over the file. No extra package needed.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


ENV_PATH = os.path.join(_HERE, ".env")
ENV_FOUND = False
ENV_KEYS = []      # the setting names read from .env (shown at startup so a wrong/unread file is obvious)


def _read_text(path):
    """.env as text whatever editor saved it: UTF-8 with or without BOM, or UTF-16 (PowerShell's > and Out-File)."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or (len(raw) > 1 and raw[1:2] == b"\x00"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def _load_env_file(path=ENV_PATH):
    global ENV_FOUND
    try:
        text = _read_text(path)
    except OSError:
        return
    ENV_FOUND = True
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.split("#", 1)[0].strip().strip("'\"")
        if key and key not in ENV_KEYS:
            ENV_KEYS.append(key)
        os.environ.setdefault(key, value)


_load_env_file()

API = os.environ.get("RIT_API", "http://localhost:9999/v1")
API_KEY = os.environ.get("RIT_API_KEY", "Rotman")
HEADERS = {"X-API-Key": API_KEY}
