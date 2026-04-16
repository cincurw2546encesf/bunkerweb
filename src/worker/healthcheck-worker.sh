#!/bin/bash

celery -A worker.app inspect ping \
  --destination "worker@${HOSTNAME:-$(hostname)}" \
  --timeout 10 2>/dev/null | grep -q "pong"
