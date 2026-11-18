"""
Compare versions of vol_algo.py on REAL heats you logged (see replay_rit_vol.py).

vol_algo saves each heat in its own folder (logs/vol/<date_time>/), which is exactly what this takes:

    python replay_compare.py logs/vol/20261117_171049 logs/vol/20261117_172511 --refs main WORKTREE --repeats 4

Older flat files (vol_log_X.csv + news_raw_X.txt) can be sorted into that layout with organize_logs.py.

--refs are git refs (branch, tag, commit) or WORKTREE for the files on disk now. Each version is run
--repeats times per heat with a small vol jitter (see replay_rit_vol.py): the algo is path-sensitive, so
one replay is one sample. Repeat k uses the same jitter for every version, so the comparison is paired. The per-heat difference between versions is what to look at, not the totals.
"""
import argparse
import glob
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PAT = re.compile(r"final P&L=([-\d,\.]+) \(fees ([-\d,\.]+), penalty ([-\d,\.]+)\)")
LIVE = re.compile(r"live P&L at t=(\d+): ([-\d,\.]+|nan)")


def heat_files(folder):
    """A heat folder as vol_algo writes it (logs/vol/<date_time>/: vol_log.csv + news_raw.txt + console.txt),
    or older files (vol_log_X.csv + news_raw_X.txt or a saved console log)."""
    csvs = sorted(glob.glob(os.path.join(folder, "vol_log*.csv")))
    news = (sorted(glob.glob(os.path.join(folder, "news_raw*.txt"))) or
            sorted(glob.glob(os.path.join(folder, "console*.txt"))) or sorted(glob.glob(os.path.join(folder, "*.txt"))))
    if len(csvs) != 1:
        sys.exit(f"{folder}: need exactly one vol_log csv (plus optionally news_raw / console .txt)")
    return csvs[0], (news[0] if news else None)      # no news file: replay infers it from fair_vol


def checkout(ref, tmp):
    if ref == "WORKTREE":
        return HERE
    if os.path.isdir(ref):                       # a folder holding a vol_algo.py (e.g. a copy with other defaults)
        return os.path.abspath(ref)
    path = os.path.join(tmp, re.sub(r"\W", "_", ref))
    subprocess.run(["git", "-C", HERE, "worktree", "add", "-q", "--detach", path, ref], check=True)
    return path


def run_batch(jobs, tick, loop_sleep):
    """jobs: [(label, heat, code_dir, csv, news)] run in parallel; returns [(label, heat, pnl, fees, pen, live)]."""
    procs = []
    for j, (label, heat, code, csv_path, news, rep) in enumerate(jobs):
        port = 9800 + j
        args = ["--csv", csv_path, "--port", str(port), "--tick", str(tick), "--start-delay", "1.5",
                "--jitter-seed", str(rep)] + (["--news", news] if news else [])
        srv = subprocess.Popen([sys.executable, os.path.join(HERE, "replay_rit_vol.py")] + args,
                               cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append((label, heat, code, port, srv))
    time.sleep(0.7)
    algos = []
    for label, heat, code, port, srv in procs:
        wd = tempfile.mkdtemp(prefix="replay_")
        env = dict(os.environ, VOL_LOG_DIR=os.path.join(wd, "logs"), RIT_API=f"http://127.0.0.1:{port}/v1", RIT_API_KEY="Rotman", LOOP_SLEEP=str(loop_sleep))
        algos.append(subprocess.Popen([sys.executable, os.path.join(code, "vol_algo.py")], cwd=wd, env=env,
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    out = []
    for (label, heat, code, port, srv), algo in zip(procs, algos):
        try:
            txt = srv.communicate(timeout=300 * tick + 30)[0]
        except subprocess.TimeoutExpired:
            srv.kill()
            txt = srv.communicate()[0]
        algo.kill()
        m, lv = PAT.search(txt), LIVE.search(txt)
        vals = tuple(float(x.replace(",", "")) for x in m.groups()) if m else (None, None, None)
        live = float(lv.group(2).replace(",", "")) if lv and lv.group(2) != "nan" else None
        out.append((label, heat) + vals + (live,))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("heats", nargs="+", help="folders, one per logged heat")
    ap.add_argument("--refs", nargs="+", default=["HEAD", "WORKTREE"])
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--parallel", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--tick", type=float, default=0.2, help="seconds per replayed tick (1.0 = real speed)")
    ap.add_argument("--loop-sleep", type=float, default=None,
                    help="algo LOOP_SLEEP; default scales with --tick so loops per tick match live (0.2s at 1s/tick)")
    a = ap.parse_args()
    files = {h: heat_files(h) for h in a.heats}
    tmp = tempfile.mkdtemp(prefix="replay_refs_")
    codes = {}
    try:
        for r in a.refs:
            codes[r] = checkout(r, tmp)
        # same jitter seed for every version within a repeat: versions see identical quotes (paired)
        jobs = [(r, h, codes[r]) + files[h] + (k,) for k in range(a.repeats) for h in a.heats for r in a.refs]
        res = []
        for i in range(0, len(jobs), a.parallel):
            res += run_batch(jobs[i:i + a.parallel], a.tick, a.loop_sleep if a.loop_sleep is not None else 0.25 * a.tick)
            print(f"  {min(i + a.parallel, len(jobs))}/{len(jobs)} runs done", flush=True)
    finally:
        for r, path in codes.items():
            if path != HERE and not os.path.isdir(r):
                subprocess.run(["git", "-C", HERE, "worktree", "remove", "--force", path])
    for x in sorted(res, key=lambda x: (x[1], x[0])):
        print(f"  {os.path.basename(x[1].rstrip('/')):<12} {x[0][:14]:<14} " +
              (f"P&L {x[2]:>10,.0f}  fees {x[3]:>8,.0f}  penalty {x[4]:>6,.0f}" if x[2] is not None else "FAILED"))
    print(f"\n{'heat':<22}{'live':>10}" + "".join(f"{r[:14]:>26}" for r in a.refs))
    diffs = []
    for h in a.heats:
        live = next((x[5] for x in res if x[1] == h and x[5] is not None), None)
        cells, means = [], []
        for r in a.refs:
            p = [x[2] for x in res if x[0] == r and x[1] == h and x[2] is not None]
            means.append(statistics.mean(p) if p else None)
            sd = statistics.stdev(p) if len(p) > 1 else 0
            cells.append(f"{statistics.mean(p):>14,.0f} (sd {sd:>6,.0f})" if p else f"{'failed':>24}")
        if len(means) >= 2 and None not in means[:2]:
            diffs.append(means[-1] - means[0])
        print(f"{os.path.basename(h.rstrip('/')):<22}{(f'{live:,.0f}' if live is not None else '-'):>10}" + "".join(f"{c:>26}" for c in cells))
    if diffs:
        sd = statistics.stdev(diffs) / len(diffs) ** 0.5 if len(diffs) > 1 else float("nan")
        print(f"\n{a.refs[-1]} minus {a.refs[0]}: mean {statistics.mean(diffs):+,.0f} per heat (±{sd:,.0f} s.e.), "
              f"better on {sum(d > 0 for d in diffs)}/{len(diffs)} heats")
    print("'live' = the P&L vol_algo logged in that heat on the real server (last logged tick).")


if __name__ == "__main__":
    main()
