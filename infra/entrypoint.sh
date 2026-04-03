#!/bin/bash
# Copy read-only mounted config files to writable working copies
[ -f /mnt/claude.json ] && cp /mnt/claude.json /root/.claude.json

exec "$@"
