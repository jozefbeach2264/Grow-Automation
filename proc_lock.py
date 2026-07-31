"""
Interprocess mutual exclusion for the grow controller.

Several entry points can drive the same hardware at the same time: the poller,
the supervised bucket-calibration harnesses (`bucket_ai_dose_test.py`,
`bucket_dose_test.py`), and operator one-shots. Nothing in the system stopped two
of them from passing the same preflight and acting concurrently -- and the
crash-recovery record (`runtime_state.active_dose`) is a SINGLE slot, so two
overlapping doses silently overwrite each other's record: the watchdog then
vouches for the wrong port, the crash estimate under-counts, and the first pump
to finish clears the record that was protecting the second.

This module is the missing mutex. It uses `fcntl.flock` advisory locks, which
the KERNEL releases when the holding process dies -- so a crash can never leave
a stale lock that wedges dosing forever (the failure mode a pidfile would have).

Locks are non-blocking by design: a control loop must never park waiting on
another process. `LockBusy` is the answer, and the caller decides (dosing
refuses the dose; the poller refuses to start a second instance).

Lock files live in `profiles/` next to the other runtime state and hold the
holder's pid purely for human diagnosis -- the lock itself is the flock, never
the file contents.
"""

import errno
import fcntl
import os
from pathlib import Path

_LOCK_DIR = Path(__file__).parent / "profiles"

# Only one chemical dose may be in flight system-wide. The name is shared by
# timed_dose and timed_dose_pair -- a pair dose and a pH dose are equally
# exclusive, and the single active-dose record they both write demands it.
CHEMICAL_DOSE = "chemical-dose"

# One poller per machine. A second instance would double-drive every deterministic
# emergency and race the first on every write.
POLLER = "poller"


class LockBusy(RuntimeError):
    """Another live process holds the lock. Carries the holder's pid when readable."""


def lock_path(name: str) -> Path:
    return _LOCK_DIR / f".{name}.lock"


class ProcessLock:
    """Exclusive, non-blocking, crash-safe interprocess lock.

    Two usage shapes, both supported:

        with ProcessLock(proc_lock.CHEMICAL_DOSE):   # scoped -- released on exit
            ...

        lock = ProcessLock(proc_lock.POLLER)         # process-lifetime
        lock.acquire()

    `acquire()` raises LockBusy immediately if another process holds it. Re-entry
    from the SAME process also raises: flock binds to the open file description,
    so a second open() in this process conflicts like any other. That is
    deliberate -- nested dosing would defeat the point of the mutex.
    """

    def __init__(self, name: str):
        self.name = name
        self.path = lock_path(name)
        self._fh = None

    # -- context manager ---------------------------------------------------- #
    def __enter__(self) -> "ProcessLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    # -- explicit API ------------------------------------------------------- #
    def acquire(self) -> "ProcessLock":
        """Take the lock or raise LockBusy. Never blocks."""
        self.path.parent.mkdir(exist_ok=True)
        # "a+" so an existing holder's pid stays readable until WE own the lock --
        # opening "w" would truncate the file before knowing whether we can have it.
        fh = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            holder = self.holder_pid()
            fh.close()
            if e.errno in (errno.EACCES, errno.EAGAIN):
                raise LockBusy(
                    f"{self.path.name} is held by pid {holder}"
                ) from e
            raise
        except Exception:
            fh.close()
            raise
        self._fh = fh
        # Stamp our pid for diagnosis only. A write failure here is NOT a lock
        # failure -- we already hold the flock, which is the actual guarantee.
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(f"{os.getpid()}\n")
            fh.flush()
        except Exception:
            pass
        return self

    def release(self) -> None:
        """Drop the lock. Safe to call twice; a no-op if never acquired."""
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass       # closing the fd releases it regardless
        finally:
            try:
                fh.close()
            except Exception:
                pass

    def holder_pid(self) -> str:
        """Pid recorded in the lock file, or '?' -- diagnosis only, never a gate.
        The file can lag reality (the kernel frees the flock without touching it),
        so this is a hint for a human, not something to make decisions on."""
        try:
            return self.path.read_text(encoding="utf-8").strip().splitlines()[0] or "?"
        except Exception:
            return "?"

    @property
    def held(self) -> bool:
        return self._fh is not None
