"""Plain, aligned terminal output for the algos (one event per line, works in any terminal width).

    t= 76 | NEWS    | Week 2 vol is 31.0%
    t= 77 | SELL    |  400 x RTM50C  edge $23/contract
    t= 80 | status  | RTM  49.71  vol 31.0% vs mkt 26.0% ...
"""
import sys
import threading

_lock = threading.Lock()
_logf = None


def set_log_file(path):
    """Also append everything printed to this file (so a heat's terminal output is never lost)."""
    global _logf
    _logf = open(path, "a", encoding="utf-8")


def say(msg=""):
    with _lock:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
        if _logf:
            _logf.write(msg + "\n")
            _logf.flush()


def event(tick, tag, msg):
    say(f"t={tick:3d} | {tag:<8}| {msg}")


def banner(msg):
    say("")
    say("=" * 72)
    for line in msg.split("\n"):
        say(line)
    say("=" * 72)
