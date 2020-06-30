Sync Downloaded Files
=====================

This is a Work-In-Progress.

A program to do an rsync (via SSH) from a remote server to a local directory.

This is meant to be run using tmux/screen as it just outputs status continuously.

Features:
  * Can parse the output of rsync '--progress' output and detect if the
    transfer rate is slow. Will terminate rsync if transfer rate is slow for
    too long.

Install
-------

Using Poetry https://python-poetry.org/ you would do the following::

    $ poetry build
    $ pip install --user dist/sync_downloaded_files-*whl

TODO:
  * Setup tox.ini to run mypy, black, and maybe flake8
  * Create unit tests
