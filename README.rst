Sync Downloaded Files
=====================

This is a Work-In-Progress.

A program to do an rsync (via SSH) from a remote server to a local directory.

This is meant to be run using tmux/screen as it just outputs status continuously.

Features:
  * Can parse the output of rsync '--progress' output and detect if the
    transfer rate is slow. Will terminate rsync if transfer rate is slow for
    too long.
