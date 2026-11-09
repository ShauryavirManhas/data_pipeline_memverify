"""Diagnose tests that pass on official Docker but fail in a Modal sandbox.

Runs a few named tests in one Modal sandbox of a task image, under
several environments, and prints the sandbox's environment variables.
A few minutes, not a full test suite.

Usage:
    python scripts/modal_diag.py conan-io__conan-17092 \
        test/integration/toolchains/gnu/test_autotoolsdeps.py::test_cpp_info_aggregation \
        test/integration/cache/backup_sources_test.py::TestDownloadCacheBackupSources::test_ok_when_origin_authorization_error
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memverify.dataset import docker_image_name  # noqa: E402
from memverify.scorer import ModalBackend  # noqa: E402


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print(__doc__)
        return 1
    instance_id, tests = argv[0], argv[1:]
    quoted = " ".join(shlex.quote(t) for t in tests)
    pytest = f"python -m pytest -rA -p no:cacheprovider -q {quoted} 2>&1 | grep -E '^(PASSED|FAILED|ERROR)|AssertionError|assert |Error:' | head -20"
    script = f"""
exec 2>&1
cd /testbed
echo '##### 1. environment variables (sorted)'
env | sort | grep -v -E 'TOKEN|SECRET|KEY'
echo '##### whoami / nproc / python'
whoami; nproc; python --version; command -v python
echo '##### 2. as-is, run 1'
{pytest}
echo '##### 3. as-is, run 2'
{pytest}
echo '##### 4. PYTHONHASHSEED=0'
PYTHONHASHSEED=0 {pytest}
echo '##### 5. clean environment (only PATH and HOME kept)'
env -i PATH="$PATH" HOME="${{HOME:-/root}}" bash -c {shlex.quote(pytest)}
echo '##### done'
"""
    image = docker_image_name(instance_id)
    print(f"image: {image}", flush=True)
    result = ModalBackend(cpu=2.0, memory_mb=8192).run(image, script, 900)
    print(result.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
