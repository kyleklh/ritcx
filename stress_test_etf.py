"""Stress test for etf_algo.py: many mock heats under different market assumptions,
compared against the provided base script (accepts every tender, never hedges).

    python stress_test_etf.py                # full grid (~35 min)
    python stress_test_etf.py --heats 8

The base script comparison needs test/base_etf.py: the provided base script with its API line changed to
    API = os.environ.get("RIT_API", "http://localhost:9999/v1")
Results: test/stress_results_etf.csv"""
import argparse
import csv
import os
import re
import statistics
import subprocess
import sys
import time

SCENARIOS = {
    "baseline": [],
    "thin books (depth x0.5)": ["--depth-mult", "0.5"],
    "stingy tenders (avg +0.03)": ["--gift-mean", "0.03"],
    "volatile prices (x2)": ["--vol-mult", "2"],
    "slow limit fills (x0.33)": ["--flow-mult", "0.33"],
    "tight limits (150k/50k)": ["--gross", "150000", "--net", "50000"],
    "big ETF shocks (x2)": ["--shock-mult", "2"],
}
HERE = os.path.dirname(os.path.abspath(__file__))
ALGOS = {"our algo": os.path.join(HERE, "etf_algo.py"), "base script": os.path.join(HERE, "test", "base_etf.py")}
PAT = re.compile(r"heat over\. P&L=([-\d,\.]+) CAD")


def run_batch(seeds, mock_args, algo_path, parallel=8):
    out = []
    for i in range(0, len(seeds), parallel):
        chunk = seeds[i:i + parallel]
        mocks, algos = [], []
        for j, seed in enumerate(chunk):
            mocks.append(subprocess.Popen([sys.executable, "mock_rit_etf.py", "--port", str(9600 + j), "--tick", "0.2",
                                           "--seed", str(seed), "--start-delay", "1"] + mock_args,
                                          cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        time.sleep(0.5)
        for j, seed in enumerate(chunk):
            env = dict(os.environ, RIT_API=f"http://localhost:{9600 + j}/v1", LOOP_SLEEP="0.03")
            algos.append(subprocess.Popen([sys.executable, algo_path], cwd=os.path.join(HERE, "test"), env=env,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(68)
        for a in algos:
            a.kill()
        for seed, m in zip(chunk, mocks):
            m.kill()
            mt = PAT.search(m.communicate()[0])
            out.append((seed, float(mt.group(1).replace(",", "")) if mt else None))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heats", type=int, default=16)
    ap.add_argument("--first-seed", type=int, default=700)
    ap.add_argument("--algos", default="our algo,base script")
    a = ap.parse_args()
    os.makedirs(os.path.join(HERE, "test"), exist_ok=True)
    seeds = list(range(a.first_seed, a.first_seed + a.heats))
    rows = []
    for name, margs in SCENARIOS.items():
        for algo in a.algos.split(","):
            res = run_batch(seeds, margs, ALGOS[algo])
            for seed, pnl in res:
                rows.append({"scenario": name, "algo": algo, "seed": seed, "pnl": pnl})
            ok = [p for _, p in res if p is not None]
            if ok and all(x == 0 for x in ok):
                sys.exit("All heats ended at exactly 0: the algo probably failed to start. Aborting.")
            print(f"{name:<28} {algo:<12}: mean {statistics.mean(ok):>9,.0f}  min {min(ok):>9,.0f}  "
                  f"stdev {statistics.pstdev(ok):>8,.0f}  losing {sum(x < 0 for x in ok)}/{len(ok)}", flush=True)
    with open(os.path.join(HERE, "test", "stress_results_etf.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
