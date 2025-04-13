#!/bin/bash

echo "Waiting for services to be ready..."

# Function to check if a host is resolvable
check_host() {
  getent hosts $1 > /dev/null 2>&1
  return $?
}

# Wait for order-service
until check_host order-service; do
  echo "Waiting for order-service DNS resolution..."
  sleep 5
done

# Wait for stock-service
until check_host stock-service; do
  echo "Waiting for stock-service DNS resolution..."
  sleep 5
done

# Wait for payment-service
until check_host payment-service; do
  echo "Waiting for payment-service DNS resolution..."
  sleep 5
done

echo "All services are ready! Starting Nginx..."
exec nginx -g 'daemon off;'