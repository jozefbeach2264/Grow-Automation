#!/usr/bin/env python3
"""
Self-tests for the interprocess lock (proc_lock.py). No hardware. Covers the
properties the chemical-dose mutex and the poller singleton actually rely on:
mutual exclusion across REAL processes, and kernel release when a holder dies
(the crash-safety a pidfile would not give us). Lock files go to a temp dir.
Run: python3 proc_lock_test.py
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

_TMP = Path(tempfile.mkdtemp(prefix="proclock_test_"))

import proc_lock
proc_lock._LOCK_DIR = _TMP

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}")


def _busy(name: str) -> bool:
    """True if acquiring `name` raises LockBusy right now."""
    try:
        lock = proc_lock.ProcessLock(name)
        lock.acquire()
        lock.release()
        return False
    except proc_lock.LockBusy:
        return True


# =========================================================================== #
print("\n== acquire / release ==")
lock = proc_lock.ProcessLock("t-basic")
check("not held before acquire", lock.held is False)
lock.acquire()
check("held after acquire", lock.held is True)
check("lock file created in the configured dir", lock.path.parent == _TMP)
check("holder pid recorded for diagnosis", lock.holder_pid() == str(os.getpid()))
lock.release()
check("not held after release", lock.held is False)
check("release is idempotent", (lock.release(), True)[1])


print("\n== mutual exclusion (same process, second fd) ==")
outer = proc_lock.ProcessLock("t-excl").acquire()
check("second acquire of the same name is refused", _busy("t-excl") is True)
check("a DIFFERENT name is unaffected", _busy("t-other") is False)
outer.release()
check("name is free again after release", _busy("t-excl") is False)


print("\n== context manager ==")
with proc_lock.ProcessLock("t-ctx"):
    check("held inside the with-block", _busy("t-ctx") is True)
check("released on normal exit", _busy("t-ctx") is False)

try:
    with proc_lock.ProcessLock("t-ctx"):
        raise RuntimeError("boom")
except RuntimeError:
    pass
check("released even when the block raises", _busy("t-ctx") is False)

# LockBusy must escape __enter__ so callers can refuse the work.
outer = proc_lock.ProcessLock("t-ctx").acquire()
_raised = False
try:
    with proc_lock.ProcessLock("t-ctx"):
        pass
except proc_lock.LockBusy:
    _raised = True
check("with-statement raises LockBusy when already held", _raised is True)
outer.release()


print("\n== mutual exclusion across REAL processes ==")
_CHILD = f"""
import sys, time
sys.path.insert(0, {str(Path(__file__).parent)!r})
import proc_lock
proc_lock._LOCK_DIR = __import__("pathlib").Path({str(_TMP)!r})
lock = proc_lock.ProcessLock("t-proc").acquire()
print("ACQUIRED", flush=True)
time.sleep(120)
"""
child = subprocess.Popen([sys.executable, "-c", _CHILD], stdout=subprocess.PIPE, text=True)
try:
    ready = child.stdout.readline().strip()
    check("child process acquired the lock", ready == "ACQUIRED")
    check("parent is refused while the child holds it", _busy("t-proc") is True)
    check("lock file names the CHILD as holder",
          proc_lock.ProcessLock("t-proc").holder_pid() == str(child.pid))

    # The property a pidfile cannot give us: SIGKILL leaves no chance to clean up,
    # yet the kernel must drop the flock -- otherwise one crashed doser would wedge
    # every future dose until someone deleted a stale file by hand.
    child.kill()
    child.wait(timeout=10)
    freed = False
    for _ in range(50):                      # the fd teardown is not instantaneous
        if not _busy("t-proc"):
            freed = True
            break
        time.sleep(0.1)
    check("SIGKILLed holder frees the lock (no stale-lock wedge)", freed is True)
    check("stale lock FILE still exists (it is not the lock)",
          proc_lock.lock_path("t-proc").exists())
finally:
    if child.poll() is None:
        child.kill()
        child.wait(timeout=10)


print("\n== named locks are distinct ==")
a = proc_lock.ProcessLock(proc_lock.CHEMICAL_DOSE).acquire()
check("poller lock is not blocked by the dose lock", _busy(proc_lock.POLLER) is False)
a.release()
check("dose and poller lock names differ",
      proc_lock.CHEMICAL_DOSE != proc_lock.POLLER)


# =========================================================================== #
print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
import shutil
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(1 if _FAIL else 0)
