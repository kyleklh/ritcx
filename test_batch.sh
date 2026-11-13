#!/bin/bash
# Run the algo against N mock heats in parallel at fast speed. Usage: ./test_batch.sh [N] [first_seed]
N=${1:-6}; FIRST=${2:-1}
cd "$(dirname "$0")"; mkdir -p test
for i in $(seq 0 $((N-1))); do
  seed=$((FIRST+i)); port=$((9900+i))
  python3 mock_rit_vol.py --port $port --tick 0.2 --seed $seed --start-delay 1 > test/mock_$seed.out 2>&1 &
  echo $! >> test/pids
done
sleep 0.5
for i in $(seq 0 $((N-1))); do
  seed=$((FIRST+i)); port=$((9900+i))
  (RIT_API=http://localhost:$port/v1 LOOP_SLEEP=0.05 timeout 68 python3 vol_algo.py < /dev/null > test/algo_$seed.out 2>&1) &
done
sleep 70
kill $(cat test/pids) 2>/dev/null; rm -f test/pids
for i in $(seq 0 $((N-1))); do grep -h "heat over" test/mock_$((FIRST+i)).out || echo "seed $((FIRST+i)): no result"; done
