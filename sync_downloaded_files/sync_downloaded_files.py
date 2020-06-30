#!/usr/bin/python3 -tt
# vim: ai ts=4 sts=4 et sw=4

# Copyright (C) 2020 John L. Villalovos (john@sodarock.com)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import argparse
import contextlib
import datetime
import locale
import os
import pty
import re
import select
import subprocess
import sys
import time
from typing import Iterator, List, NamedTuple, Optional

import attr


locale.setlocale(locale.LC_ALL, "en_US.UTF-8")

RATE_LOW_TIMEOUT = 30.0
TRANSFER_RATE_MIN = 100_000


def main() -> int:
    args = parse_args()
    result = execute_rsync(args)
    return result


def execute_rsync(args: argparse.Namespace) -> int:
    """
    Setup our rsync command line and then execute rsync. If a repeate_time has
    been given then repeat forever.
    """
    cmd_list = []

    cmd_list.extend(
        [
            "rsync",
            "--hard-links",
            "--links",
            "--partial",
            "--perms",
            "--progress",
            "--recursive",
            "--times",
            "--verbose",
            "--timeout=30",
            "--outbuf=Line",
            # '--owner',
            # '--group',
        ]
    )
    if args.jump_host:
        cmd_list.extend(["-e", "ssh -J {}".format(args.jump_host)])
    if args.exclude_file:
        cmd_list.extend(["--exclude-from={}".format(args.exclude_file)])
    if args.bwlimit:
        cmd_list.extend(["--bwlimit", args.bwlimit])
    cmd_list.extend(["--exclude", "*.part"])
    cmd_list.extend(["{}:{}".format(args.server, args.server_path), args.dest_dir])

    result = 0

    # Create our ptys for stdout, stderr, and stdin. We have to setup ptys and
    # use them in the subprocess.Popen() call or rsync will not display the
    # progress values while running.
    with pty_open() as ptys:
        while True:
            print("Syncing from {}...".format(args.server))
            print("To: {}".format(args.dest_dir))
            print("Executing: {}".format(" ".join(cmd_list)))
            try:
                result = run_rsync_command(cmd_list, ptys)
            except KeyboardInterrupt:
                time.sleep(1.25)
                print("Program terminated with keyboard interrupt.  Exiting...")
                result = 1
                break
            except subprocess.CalledProcessError:
                # Ignore errors in call
                pass
            if args.repeat_time:
                countdown(args.repeat_time)
            else:
                break
    return result


class Ptys(NamedTuple):
    """The namedtuple to hold all of our ptys"""

    m_out: int
    s_out: int
    m_err: int
    s_err: int
    m_in: int
    s_in: int


def run_rsync_command(cmd_line: List[str], ptys: Ptys) -> int:
    process = subprocess.Popen(
        cmd_line, bufsize=0, stdin=ptys.s_in, stdout=ptys.s_out, stderr=ptys.s_err,
    )

    states = States()

    # Record the last time we had any activity from rsync as we will terminate
    # it if we think it could have got stuck.
    last_activity = time.time()

    timeout = 0.04  # seconds to wait for select.select
    while True:
        # We check if any of our ptys are ready for reading. We use the timeout
        # feature as we don't want to block forever waiting for rsync as it
        # could hang.
        ready, _, _ = select.select([ptys.m_out, ptys.m_err], [], [], timeout)
        if ready:
            last_activity = time.time()
            should_terminate = watch_rsync_progress(
                process=process, active_fds=ready, states=states
            )
            if should_terminate:
                print("Killing process as rate too low")
                process.terminate()
        elif process.poll() is not None:
            break  # p exited
        else:
            # If we don't have any activity for a minute we will terminate
            if time.time() - last_activity > 60.0:
                print(f"No activity for {time.time() - last_activity:2.2f} seconds")
                print("Terminating rsync process")
                process.terminate()
            if time.time() - last_activity > 120.0:
                # Just in case rsync didn't respond to the terminate command
                print("Killing rsync process")
                process.kill()

    # Note(jlvillal): This is to keep mypy happy.
    exit_code = process.poll()
    # This should always be True as we only break out of the while loop if
    # process.poll() is not None.
    if exit_code is not None:
        return exit_code
    # Realistically we should not get here
    raise RuntimeError("Unexpectedly got None for process.poll()")


@attr.s
class States:
    """Class used to store our various states that we need to pass back and forth"""

    # If we are currently in the state of having too low of a transfer rate
    low_xfer_rate: bool = False
    # The last time we had an acceptable rate of download
    last_acceptable_rate_time: float = time.time()
    # Last non-progress line received. This should contain the filename we are
    # currently transferring
    last_line: str = ""
    # If the last line printed was a progress line or not
    last_was_progress: bool = False


def watch_rsync_progress(
    process: subprocess.Popen, active_fds: List[int], states: States
) -> bool:
    """Watch the output from rsync

    In particular we care about the progress lines it gnerates as it gives our
    current transfer rate. If the transfer rate is too low for longer than our
    timeout we indicate we should terminate.

    Returns:
       True, if process should be terminated as transfer rate too low.
       False, if should not terminate process.
    """
    should_terminate = False
    for fd in active_fds:
        data = os.read(fd, 512)
        if not data:
            continue
        # Convert from bytes to str
        temp_string = os.fsdecode(data)
        temp_string = temp_string.strip()
        for line in temp_string.splitlines():
            if not states.last_was_progress:
                print(f"\t{line!r}")

            progress_stats = parse_progress_line(line)
            print_progress(
                progress_stats=progress_stats,
                filename=states.last_line,
                last_was_progress=states.last_was_progress,
            )
            if progress_stats:
                states.last_was_progress = True
                if progress_stats.transfer_rate > TRANSFER_RATE_MIN:
                    states.last_acceptable_rate_time = time.time()
                    states.low_xfer_rate = False
                else:
                    if not states.low_xfer_rate:
                        states.low_xfer_rate = True
                    else:
                        print(
                            "Rate is too low!!! {:2.2f} seconds".format(
                                RATE_LOW_TIMEOUT
                                - (time.time() - states.last_acceptable_rate_time)
                            )
                        )
                        states.last_was_progress = False
                        if (
                            time.time() - states.last_acceptable_rate_time
                            > RATE_LOW_TIMEOUT
                        ):
                            should_terminate = True
            else:
                if line:
                    states.last_was_progress = False
                    states.last_line = line

    return should_terminate


class RsyncProgressStatus(NamedTuple):
    bytes_transferred: int
    percent_transferred: float
    transfer_rate: int
    eta: str


def print_progress(
    *,
    progress_stats: Optional[RsyncProgressStatus],
    filename: str,
    last_was_progress: bool,
) -> None:
    """
    Print the progress statistics, if we have them. If our last line printed
    was not a progress line then print the filename.
    """
    if progress_stats:
        if not last_was_progress:
            print(filename)
        print(
            f"\r"
            f"Match!: Rate: {progress_stats.transfer_rate:n} bytes/s, "
            f"Bytes Transferred: {progress_stats.bytes_transferred:n}, "
            f"Percent Transferred: "
            f"{progress_stats.percent_transferred}%, "
            f"ETA: {progress_stats.eta}                     ",
            end="",
        )


def parse_rate(rate_string: str) -> int:
    # We are expecting an input like one of these:
    #   6.8MB/s
    #   32.4kB/s
    result = re.search(r"(?P<val>[0-9.]+)(?P<unit>[a-zA-Z]+)/s", rate_string)
    if not result:
        print("rate_string:", rate_string)
        print("rate_string NO match")
        return 0

    val = float(result.group("val"))
    unit = result.group("unit").upper()
    if unit == "KB":
        ret_val = val * 1024
    elif unit == "MB":
        ret_val = val * 1024 * 1024
    else:
        raise ValueError("Unknown unit!: {}".format(unit))
    return int(ret_val)


def countdown(seconds: int) -> None:
    stop_time = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
    print("Current time is:     {:%H:%M:%S}".format(datetime.datetime.now()))
    print("Will repeat sync at: {:%H:%M:%S}".format(stop_time))
    while datetime.datetime.now() < stop_time:
        remaining_time = stop_time - datetime.datetime.now()
        time_string = "\rTime remaining: {:02d}:{:02d}:{:02d}".format(
            remaining_time.seconds // 3600,
            (remaining_time.seconds // 60) % 60,
            remaining_time.seconds % 60,
        )
        sys.stdout.write(time_string)
        sys.stdout.flush()
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            sys.exit(1)
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync with server")
    parser.add_argument(
        "-r",
        "--repeat-time",
        type=int,
        help=(
            "How often in seconds to repeat sync, continue until interrupted by "
            "keyboard"
        ),
    )
    parser.add_argument("-l", "--bwlimit", help="Pass bwlimit value to rsync")
    parser.add_argument(
        "--exclude-file", default=None, help="Exclude file for rsync to use"
    )
    parser.add_argument("--no-exclude", action="store_true", help="Download images")
    parser.add_argument("-j", "--jump-host", default=None, help="SSH Jump host")
    parser.add_argument("-p", "--server-path", required=True, help="Server path")
    parser.add_argument("-s", "--server", required=True, help="Server name")
    parser.add_argument(
        "-d", "--dest_dir", required=True, help="Destination directory to save files"
    )

    args = parser.parse_args()
    print(args)
    args.dest_dir = os.path.abspath(os.path.expanduser(args.dest_dir)) + "/"
    if not args.server_path.endswith("/"):
        args.server_path += "/"

    if args.exclude_file:
        args.exclude_file = os.path.abspath(os.path.expanduser(args.exclude_file))
        if not os.path.isfile(args.exclude_file):
            parser.print_help()
            print()
            print(
                f"ERROR: --exclude-file value is not a file or does not exist: "
                f"{args.exclude_file}"
            )
            sys.exit(1)

    print("Downloading to: {}".format(args.dest_dir))
    return args


@contextlib.contextmanager
def pty_open() -> Iterator[Ptys]:
    """Contextmanager to open and then make sure we close the ptys we open"""
    # Open ptys for stdout, stderr, and stdin
    m_out, s_out = pty.openpty()
    m_err, s_err = pty.openpty()
    m_in, s_in = pty.openpty()
    try:
        yield Ptys(
            m_out=m_out, s_out=s_out, m_err=m_err, s_err=s_err, m_in=m_in, s_in=s_in
        )
    finally:
        for fd in m_out, s_out, m_err, s_err, m_in, s_in:
            os.close(fd)


def parse_progress_line(line: str) -> Optional[RsyncProgressStatus]:
    """
    Attempt to parse an rsync progress line into values.

    Either returns the values or None if not a progress line.
    """
    # Example rsync progress line:
    #  823,915,288  35%   36.65MB/s    0:00:40
    result = re.search(
        r"""(?P<bytes>.*[0-9,]+)            # bytes transferred
            \ +                             # one or more spaces
            (?P<percent>[0-9.]+)%           # percent complete
            \ +                             # one or more spaces
            (?P<rate>[0-9A-Za-z.]+/s)       # current rate of transfer
            \ +                             # one or more spaces
            (?P<eta>\d+:\d\d:\d\d)          # current rate of transfer
            .*
        """,
        line,
        flags=re.VERBOSE,
    )
    if not result:
        return None

    bytes_transferred = locale.atoi(result.group("bytes"))
    percent_transferred = float(result.group("percent"))
    transfer_rate = parse_rate(result.group("rate"))
    eta = result.group("eta")
    return RsyncProgressStatus(
        bytes_transferred=bytes_transferred,
        percent_transferred=percent_transferred,
        transfer_rate=transfer_rate,
        eta=eta,
    )


if "__main__" == __name__:
    sys.exit(main())
