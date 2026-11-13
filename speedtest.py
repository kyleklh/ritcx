"""Measures how fast the API answers. Run while the RIT Client (or a mock) is open:  python speedtest.py
Each call should take well under 50 ms. If 'localhost' is ~2000 ms but 127.0.0.1 is fast, that's the
Windows IPv6 delay: update the mock (git pull) or use API = "http://127.0.0.1:9999/v1" for the MOCK only."""
import time
import requests

for host in ("localhost", "127.0.0.1"):
    s = requests.Session()
    s.headers.update({"X-API-Key": "Rotman"})
    times = []
    for _ in range(5):
        t = time.time()
        try:
            r = s.get(f"http://{host}:9999/v1/case", timeout=5)
            ok = r.status_code
        except Exception as e:
            ok = f"error: {type(e).__name__}"
        times.append((time.time() - t) * 1000)
    print(f"{host:>10}: status {ok}, average {sum(times) / len(times):7.1f} ms per call")
