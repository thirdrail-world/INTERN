#!/bin/bash
set -e

cd /opt/kai/stack
BACKLOG_DIR="tickets/backlog"
MIN_TICKETS=5
REFILL_AMOUNT=10

# Count current actionable backlog (exclude gated/skipped)
current=$(ls "$BACKLOG_DIR"/*.md 2>/dev/null | wc -l)

if [ "$current" -lt "$MIN_TICKETS" ]; then
    echo "$(date): Backlog low ($current < $MIN_TICKETS), generating $REFILL_AMOUNT tickets..."
    .venv/bin/python3 tools/generate_tickets.py --limit "$REFILL_AMOUNT"
    
    # Commit so Intern's preflight doesn't fail
    git add tickets/backlog/
    git diff --cached --quiet || git commit -m "chore(auto): refill backlog with $(ls tickets/backlog/*.md 2>/dev/null | wc -l) tickets"
    
    echo "$(date): Backlog refilled"
else
    echo "$(date): Backlog healthy ($current tickets), skipping"
fi
