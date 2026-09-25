#!/bin/bash
# console-attach.sh — attaches the physical console (tty1) to whichever
# user's tmux session last connected over SSH. Runs as root (via the
# console-autoswitch.service unit) and drops to that user with `su`,
# which — unlike `sudo` on this machine (Defaults use_pty in sudoers) —
# does not reallocate a new pseudo-terminal, so the tmux client actually
# lands on tty1 instead of an invisible pty nobody is looking at.
CURRENT=$(cat /tmp/last_ssh_user 2>/dev/null)
if [ -z "$CURRENT" ]; then
    CURRENT=archer
fi
exec su - "$CURRENT" -c "tmux new-session -A -s '$CURRENT'"
