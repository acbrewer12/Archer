#!/bin/bash
# Pull latest Archer code and restart — run anytime to update
ARCHER_DIR="/opt/archer"
BRANCH="claude/archer-truck-ai-system-TlfGE"

echo "Pulling latest Archer..."
git -C "$ARCHER_DIR" pull origin "$BRANCH"
systemctl restart archer
echo "Done. Archer restarted."
