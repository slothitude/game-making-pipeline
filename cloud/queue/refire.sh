#!/bin/bash
# Settle wedged running jobs (router restarts orphan them), refire jezzball M2.
set -u
T=$(grep -oP "(?<=QUEUE_TOKEN=).*" /home/ubuntu/pipeline/queue/env)
for J in 23 30 51; do
  curl -s -X POST "http://127.0.0.1:8901/jobs/$J/result" \
    -H "X-Token: $T" -H "Content-Type: application/json" \
    -d '{"error":"settled: orphaned by router restart - refiring"}' | head -c 50
  echo " <- settled $J"
done
printf '%s' '{"type":"milestone","payload":{"game":"jezzball","milestone":"M2"},"priority":4}' > /tmp/mj.json
curl -s -X POST http://127.0.0.1:8901/jobs -H "X-Token: $T" \
  -H "Content-Type: application/json" --data @/tmp/mj.json
echo " <- fresh M2 fired"
