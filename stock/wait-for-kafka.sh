#!/bin/bash

BROKERS=("kafka-1:9091" "kafka-2:9092" "kafka-3:9093")

echo "Waiting for all Kafka brokers to be ready..."
for broker in "${BROKERS[@]}"; do
  until nc -z $(echo $broker | cut -d':' -f1) $(echo $broker | cut -d':' -f2); do
    echo "Waiting for broker $broker..."
    sleep 1
  done
done
echo "All Kafka brokers are ready."

python init_db.py
exec gunicorn -b 0.0.0.0:5000 -w 2 --timeout 30 --log-level=info --reload app:app