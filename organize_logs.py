"""
Sort old vol_algo log files into one folder per heat, the layout vol_algo now writes by itself:

    logs/vol/<date_time>_heat<N>/vol_log.csv     per-tick log for that heat only
                                 news_raw.txt    that heat's news

Older versions wrote vol_log_<date_time>.csv and news_raw_<date_time>.txt next to the script, with every heat
of a session in the same pair of files. Run this in the folder holding them (or pass it):

    python organize_logs.py                 # files in the current folder
    python organize_logs.py C:\\path\\to\\old_logs

Files are copied, never moved or deleted. A csv whose heats can't be matched to news is still split;
the replay then infers the news from the fair_vol column.
"""
import csv
import glob
import os
import re
import sys


def split_csv(path):
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None, []
    heats = [[]]
    for r in rows:
        if heats[-1] and int(r["tick"]) < int(heats[-1][-1]["tick"]):
            heats.append([])
        heats[-1].append(r)
    return list(rows[0].keys()), heats


def split_news(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    blocks = []
    for ln in lines:
        if ln.startswith("tick=1 ") or not blocks:      # the t=1 message opens every heat
            blocks.append([])
        blocks[-1].append(ln)
    return blocks


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "."
    out_root = os.path.join("logs", "vol")
    done = 0
    for path in sorted(glob.glob(os.path.join(src, "vol_log_*.csv"))):
        stamp = re.search(r"vol_log_(.+)\.csv$", os.path.basename(path)).group(1)
        keys, heats = split_csv(path)
        if not heats:
            print(f"{os.path.basename(path)}: empty, skipped")
            continue
        news = split_news(os.path.join(src, f"news_raw_{stamp}.txt"))
        if news and len(news) != len(heats):
            print(f"{os.path.basename(path)}: {len(heats)} heats but {len(news)} news blocks; "
                  f"news left out (the replay will infer it)")
            news = []
        for i, h in enumerate(heats, 1):
            folder = os.path.join(out_root, f"{stamp}_heat{i}")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "vol_log.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, keys)
                w.writeheader()
                w.writerows(h)
            if news:
                with open(os.path.join(folder, "news_raw.txt"), "w", encoding="utf-8") as f:
                    f.write("\n".join(news[i - 1]) + "\n")
            print(f"{folder}: ticks {h[0]['tick']}-{h[-1]['tick']}, final P&L {float(h[-1]['nlv']):,.0f}"
                  f"{'' if news else ' (no news file)'}")
            done += 1
    print(f"{done} heats written under {out_root}" if done else "no vol_log_*.csv files found")


if __name__ == "__main__":
    main()
