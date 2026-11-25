#!/usr/bin/env bash
# Starts the 3 Pi programs together. Ctrl+C stops all of them.
# Output of each program goes to logs/<name>.log  (watch with: tail -f logs/processor.log)
cd "$(dirname "$0")"
mkdir -p logs

python3 -u processor.py      > logs/processor.log 2>&1 &  P1=$!
python3 -u live_inference.py > logs/ml.log        2>&1 &  P2=$!
python3 -u dashboard.py      > logs/dashboard.log 2>&1 &  P3=$!

IP=$(hostname -I | awk '{print $1}')
echo "processor.py      PID $P1  -> logs/processor.log"
echo "live_inference.py PID $P2  -> logs/ml.log"
echo "dashboard.py      PID $P3  -> logs/dashboard.log"
echo
echo "Dashboard: http://$IP:5000"
echo "Press Ctrl+C to stop all three."
echo

# Show the processor output live; if any program dies, say so.
tail -n 0 -f logs/processor.log &  TAILPID=$!
trap 'echo; echo "Stopping..."; kill $P1 $P2 $P3 $TAILPID 2>/dev/null; exit 0' INT TERM
while true; do
    for pid in $P1 $P2 $P3; do
        if ! kill -0 $pid 2>/dev/null; then
            echo "!! A program stopped (PID $pid). Check the logs/ folder."
            kill $P1 $P2 $P3 $TAILPID 2>/dev/null
            exit 1
        fi
    done
    sleep 2
done
