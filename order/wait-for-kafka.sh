#!/bin/bash
set -e 

until nc -z kafka 9092; do
  echo "Waiting for Kafka to be ready..."
  sleep 1
done
echo "Kafka is ready"

echo "Waiting for database..."
until timeout 1 bash -c "cat < /dev/null > /dev/tcp/order-pgpool/5432" 2>/dev/null; do
  echo "Database is unavailable - sleeping"
  sleep 2
done

python init_db.py
exec gunicorn -b 0.0.0.0:5000 -w 2 --timeout 300 --log-level=info --reload run:app