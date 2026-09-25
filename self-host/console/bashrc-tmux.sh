# Append to the end of each account's ~/.bashrc (archer and claudecode).
#
# Drop a direct SSH login straight into that account's shared tmux session.
# Checks $SSH_TTY as well as $TMUX: `su -` and a console login wipe $TMUX,
# so checking it alone started tmux inside tmux whenever someone switched
# users in a session (the stacked green bars). The tty1 console doesn't use
# this — console-attach.sh attaches it directly.
if [ -z "$TMUX" ] && [ -n "$SSH_TTY" ]; then
    tmux new-session -A -s "$(whoami)"
fi
