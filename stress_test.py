"""Stress test for vol_algo.py: runs many mock heats under different market assumptions.

    python stress_test.py                 # full grid (takes ~25 min)
    python stress_test.py --heats 8       # quicker

Scenarios change what the market maker / news do; GAMMA_Z values change the algo.
Results go to stress_results.csv and a summary table is printed."""
import argparse
import csv
import os
import re
import statistics
import subprocess
import sys
import time

SCENARIOS = {
    "baseline (MM half-life 20)": [],
    "fast MM (half-life 5)": ["--half-life", "5"],
    "slow MM (half-life 60)": ["--half-life", "60"],
    "MM starts at random vol": ["--mm-start", "-1"],
    "50% of ranges miss true vol": ["--range-miss", "0.5"],
}
HERE = os.path.dirname(os.path.abspath(__file__))
PAT = re.compile(r"final P&L=([-\d,\.]+) \(fees ([-\d,\.]+), penalty ([-\d,\.]+)\)")


def run_batch(seeds, mock_args, env_extra, parallel=8):
    out = []
    for i in range(0, len(seeds), parallel):
        chunk = seeds[i:i + parallel]
        mocks, algos = [], []
        for j, seed in enumerate(chunk):
            port = 9500 + j
            mocks.append(subprocess.Popen([sys.executable, "mock_rit_vol.py", "--port", str(port), "--tick", "0.2",
                                           "--seed", str(seed), "--start-delay", "1"] + mock_args,
                                          cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        time.sleep(0.5)
        for j, seed in enumerate(chunk):
            env = dict(os.environ, RIT_API=f"http://localhost:{9500 + j}/v1", LOOP_SLEEP="0.05", **env_extra)
            algos.append(subprocess.Popen([sys.executable, os.path.join(HERE, "vol_algo.py")], cwd=os.path.join(HERE, "test"), env=env,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(68)
        for a in algos:
            a.kill()
        for seed, m in zip(chunk, mocks):
            m.kill()
            txt = m.communicate()[0]
            mt = PAT.search(txt)
            if mt:
                pnl, fees, pen = (float(x.replace(",", "")) for x in mt.groups())
                out.append((seed, pnl, fees, pen))
            else:
                out.append((seed, None, None, None))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heats", type=int, default=16)
    ap.add_argument("--z", default="3.0,2.0")
    ap.add_argument("--first-seed", type=int, default=500)
    a = ap.parse_args()
    os.makedirs(os.path.join(HERE, "test"), exist_ok=True)
    seeds = list(range(a.first_seed, a.first_seed + a.heats))
    rows = []
    for name, margs in SCENARIOS.items():
        for z in a.z.split(","):
            res = run_batch(seeds, margs, {"GAMMA_Z": z})
            for seed, pnl, fees, pen in res:
                rows.append({"scenario": name, "GAMMA_Z": z, "seed": seed, "pnl": pnl, "fees": fees, "penalty": pen})
            ok = [r[1] for r in res if r[1] is not None]
            if ok and all(x == 0 for x in ok):
                sys.exit("All heats ended at exactly 0: the algo probably failed to start. Aborting.")
            print(f"{name:<30} Z={z}: mean {statistics.mean(ok):>9,.0f}  min {min(ok):>9,.0f}  "
                  f"losing {sum(x < 0 for x in ok)}/{len(ok)}", flush=True)
    with open(os.path.join(HERE, "test", "stress_results.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main()
