#!/bin/bash

echo 'Waiting for PostgreSQL servers to be accessible...'

for i in {1..60}; do
  if getent hosts stock-pg-0 && getent hosts stock-pg-1; then
    echo 'Both PostgreSQL servers found in DNS. Proceeding...'
    break
  fi
  echo 'Waiting for DNS resolution...'
  sleep 2
done