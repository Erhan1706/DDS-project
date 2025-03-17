#!/bin/bash
set -e 

until nc -z kafka 9092; do
  echo "Waiting for Kafka to be ready..."
  sleep 1
done
echo "Kafka is ready"
# Change -w 1 to 2 after development
python init_db.py
exec gunicorn -b 0.0.0.0:5000 -w 2 --timeout 300 --log-level=info --reload run:app
