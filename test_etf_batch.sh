#!/bin/bash
# Run the ETF algo against N mock heats in parallel at 5x speed. Usage: ./test_etf_batch.sh [N] [first_seed]
# Set AUTO_CONVERT_MOCK=1 to simulate a human clicking every converter prompt (fake server only).
N=${1:-6}; FIRST=${2:-1}
cd "$(dirname "$0")"; mkdir -p test
for i in $(seq 0 $((N-1))); do
  seed=$((FIRST+i)); port=$((9800+i))
  python3 mock_rit_etf.py --port $port --tick 0.2 --seed $seed --start-delay 1 $MOCK_ARGS > test/etfmock_$seed.out 2>&1 &
  echo $! >> test/etfpids
done
sleep 0.5
for i in $(seq 0 $((N-1))); do
  seed=$((FIRST+i)); port=$((9800+i))
  (cd test && RIT_API=http://localhost:$port/v1 LOOP_SLEEP=0.03 timeout 68 python3 ../etf_algo.py < /dev/null > etfalgo_$seed.out 2>&1) &
done
sleep 70
kill $(cat test/etfpids) 2>/dev/null; rm -f test/etfpids
for i in $(seq 0 $((N-1))); do grep -h "heat over" test/etfmock_$((FIRST+i)).out || echo "seed $((FIRST+i)): no result"; done
