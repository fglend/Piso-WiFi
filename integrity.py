"""File integrity checker for the admin Security page.

Adapts orange-piso's tamper-check concept (there: a compiled SHA-256 binary
watching the PHP source) into something appropriate for this codebase: hash
the tracked source files on demand, compare against a baseline the operator
sets after a known-good deploy, and flag anything that changed since.

This is a detection aid for the operator, not a security boundary - a change
here does not block anything by itself.
"""
import hashlib
import logging
import os

logger = logging.getLogger(__name__)

# Directories (relative to the project root) whose source is worth watching.
# templates/ is included because a tampered page is exactly what an attacker
# with filesystem access would change; static/uploads and the database are
# deliberately excluded below since they are expected to change constantly.
BASELINE_DIRS = ('.', 'routes', 'network', 'templates')
BASELINE_EXTENSIONS = ('.py', '.html')
EXCLUDE_DIR_NAMES = {
    '__pycache__', '.git', '.pytest_cache', 'venv', '.venv', 'node_modules',
    'static', 'config', 'tests', '.agents', '.claude', '.codex',
}


def _iter_tracked_files(root_dir):
    for base in BASELINE_DIRS:
        base_path = os.path.join(root_dir, base)
        if not os.path.isdir(base_path):
            continue
        for dirpath, dirnames, filenames in os.walk(base_path):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_NAMES]
            # Only recurse into subdirectories for '.', since routes/network/
            # templates are already narrow, single-purpose directories.
            if base != '.':
                dirnames[:] = []
            for filename in filenames:
                if not filename.endswith(BASELINE_EXTENSIONS):
                    continue
                full_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(full_path, root_dir).replace(os.sep, '/')
                yield rel_path, full_path


def compute_hashes(root_dir='.'):
    """{relative_path: sha256_hex} for every tracked source file."""
    hashes = {}
    for rel_path, full_path in _iter_tracked_files(root_dir):
        try:
            with open(full_path, 'rb') as f:
                hashes[rel_path] = hashlib.sha256(f.read()).hexdigest()
        except OSError as exc:
            logger.warning("Could not hash %s: %s", full_path, exc)
    return hashes


def check_integrity(baseline, root_dir='.'):
    """Compare the current tree against a stored baseline.

    Returns {'ok': bool, 'changed': [...], 'missing': [...], 'new': [...]}
    sorted for stable display. 'new' is informational, not a warning sign -
    files added since the baseline was set (e.g. this very feature's own
    rollout) are expected and are not tamper evidence by themselves.
    """
    current = compute_hashes(root_dir)
    changed = sorted(
        path for path, sha in baseline.items()
        if path in current and current[path] != sha)
    missing = sorted(path for path in baseline if path not in current)
    new = sorted(path for path in current if path not in baseline)
    return {
        'ok': not changed and not missing,
        'changed': changed,
        'missing': missing,
        'new': new,
    }
