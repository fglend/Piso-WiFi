"""Shell-free command execution.

Every external command runs with an argument list (never shell=True) so
values that originate from HTTP requests (MAC addresses, IPs) can never be
interpreted by a shell.
"""
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


class CommandError(Exception):
    def __init__(self, cmd_args, returncode, stderr):
        self.cmd = cmd_args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"Command {' '.join(cmd_args)} failed ({returncode}): {stderr.strip()}")


def run_cmd(args, ignore_errors=False):
    """Run a command from an argument list and return stdout.

    Raises CommandError on non-zero exit unless ignore_errors is True.
    """
    if isinstance(args, str):
        raise TypeError("run_cmd requires an argument list, not a string")

    args = [str(a) for a in args]
    logger.debug(f"Executing: {' '.join(args)}")
    result = subprocess.run(args, capture_output=True, universal_newlines=True)

    if result.stdout:
        logger.debug(f"stdout: {result.stdout.strip()}")
    if result.stderr:
        logger.debug(f"stderr: {result.stderr.strip()}")

    if result.returncode != 0 and not ignore_errors:
        raise CommandError(args, result.returncode, result.stderr or '')
    return result.stdout


def command_exists(cmd):
    return shutil.which(cmd) is not None
