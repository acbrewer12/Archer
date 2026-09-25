#!/bin/bash
# Run by pam_exec on every SSH session (see /etc/pam.d/sshd). Records who
# logged in and sends the physical console over to their tmux session.
echo "$PAM_USER" > /tmp/last_ssh_user
# Force the physical console to immediately re-attach to this user's tmux
# session. --no-block: don't hold up the SSH login while systemd restarts it.
systemctl --no-block restart console-autoswitch.service
