#!/bin/bash
# Wait for kafka-1
until nc -z kafka-1 9092; do
  echo "Waiting for kafka-1 to be ready..."
  sleep 1
done

# Wait for kafka-2
until nc -z kafka-2 9092; do
  echo "Waiting for kafka-2 to be ready..."
  sleep 1
done

echo "Kafka is ready"
python init_db.py
exec gunicorn -b 0.0.0.0:5000 -w 2 --timeout 30 --log-level=info --reload app:app
