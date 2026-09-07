"""Shell-free command execution.

Every external command runs with an argument list (never shell=True) so
values that originate from HTTP requests (MAC addresses, IPs) can never be
interpreted by a shell.
"""
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


# A hung child (blocked on a contended xtables/ip lock, a wedged driver
# ioctl, etc.) used to block this call forever - with only one gunicorn
# worker, that froze the whole portal until someone noticed and restarted
# the service. Every command gets a hard ceiling instead.
DEFAULT_TIMEOUT = 10
# iptables/ip6tables commands, specifically, wait for the shared xtables
# lock rather than take it. If the metering thread and a portal request both
# shell out at once, one call now gets '-w' seconds queued instead of racing.
IPTABLES_WAIT_SECONDS = '5'
_LOCKING_COMMANDS = {'iptables', 'ip6tables'}


class CommandError(Exception):
    def __init__(self, cmd_args, returncode, stderr):
        self.cmd = cmd_args
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"Command {' '.join(cmd_args)} failed ({returncode}): {stderr.strip()}")


class CommandTimeout(CommandError):
    def __init__(self, cmd_args, timeout):
        self.cmd = cmd_args
        self.returncode = None
        self.stderr = f"timed out after {timeout}s"
        Exception.__init__(
            self, f"Command {' '.join(cmd_args)} timed out after {timeout}s")


def run_cmd(args, ignore_errors=False, timeout=DEFAULT_TIMEOUT):
    """Run a command from an argument list and return stdout.

    Raises CommandError on non-zero exit unless ignore_errors is True, and
    CommandTimeout (a CommandError) if it does not finish within `timeout`
    seconds - a hang here must never freeze the calling thread.
    """
    if isinstance(args, str):
        raise TypeError("run_cmd requires an argument list, not a string")

    args = [str(a) for a in args]
    if args and args[0] in _LOCKING_COMMANDS and '-w' not in args:
        args = [args[0], '-w', IPTABLES_WAIT_SECONDS, *args[1:]]
    logger.debug(f"Executing: {' '.join(args)}")
    try:
        result = subprocess.run(
            args, capture_output=True, universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after {timeout}s: {' '.join(args)}")
        if ignore_errors:
            return ''
        raise CommandTimeout(args, timeout)

    if result.stdout:
        logger.debug(f"stdout: {result.stdout.strip()}")
    if result.stderr:
        logger.debug(f"stderr: {result.stderr.strip()}")

    if result.returncode != 0 and not ignore_errors:
        raise CommandError(args, result.returncode, result.stderr or '')
    return result.stdout


def command_exists(cmd):
    return shutil.which(cmd) is not None
