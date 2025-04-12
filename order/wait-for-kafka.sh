#!/bin/bash
set -e

BROKERS=("kafka-1:9091" "kafka-2:9092" "kafka-3:9093")

echo "Waiting for all Kafka brokers to be ready..."
for broker in "${BROKERS[@]}"; do
  until nc -z $(echo $broker | cut -d':' -f1) $(echo $broker | cut -d':' -f2); do
    echo "Waiting for broker $broker..."
    sleep 1
  done
done
echo "All Kafka brokers are ready."

#TOPICS=(
#  "compensate_payment_details"
#  "compensate_stock_details"
#  "payment_details_failure"
#  "payment_details_success"
#  "stock_details_failure"
#  "stock_details_success"
#  "verify_payment_details"
#  "verify_stock_details"
#)
#
#for topic in "${TOPICS[@]}"; do
#  echo "Creating topic: $topic"
#  kafka-topics --create --if-not-exists --bootstrap-server kafka-1:9091,kafka-2:9092,kafka-3:9093 \
#    --replication-factor 3 --partitions 8 --topic "$topic"
#done
#echo "All topics created."

python init_db.py

exec gunicorn -b 0.0.0.0:5000 -w 2 --timeout 300 --log-level=info --reload run:app
