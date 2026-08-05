"""Keep long harvests inside the machine's RAM budget.

Written after a harvest run took the whole desktop down with it. Two harvests
were in flight at once on a 7 GB box: a headless Chromium rendering myScheme,
and the static fetcher running twelve threads that each could hold a 40 MB PDF
body *and* hand it to pdfplumber, whose page objects cost hundreds of megabytes
on a document that size. Twelve of those in parallel is several gigabytes of
peak, and none of it was bounded by anything.

The fix is three cheap mechanisms rather than one clever one:

- `available_mb()` reads MemAvailable straight from /proc, so there is no new
  dependency and the number is the kernel's own estimate rather than a guess
  derived from free/cached.
- `Throttle` is a semaphore around the memory-hungry stage. It caps peak usage
  at (slots x worst case) regardless of how many workers are running, which
  keeps worker count a throughput dial instead of a crash risk.
- `wait_for_memory()` is the backstop for everything unaccounted for — a
  browser that grows, an editor that opens, another job started by hand. It
  parks the caller until headroom returns rather than letting the OOM killer
  pick the victim.

None of this makes a harvest faster. It makes it finish.
"""
from __future__ import annotations

import contextlib
import ctypes
import threading
import time

MEMINFO = "/proc/meminfo"

# Floor below which we stop starting new expensive work. The desktop this runs
# on idles around 2.5 GB used with the editor open; 900 MB of headroom leaves
# room for one in-flight document plus whatever the browser is doing, while
# still being reachable enough that we do not stall on a healthy machine.
DEFAULT_FLOOR_MB = 900


def trim_heap() -> None:
    """Hand glibc's free heap back to the OS.

    Python releasing an object does not mean the process gives the memory back:
    glibc keeps freed blocks in per-arena free lists, and a workload that
    cycles megabyte-sized strings — a page of HTML, then the next — fragments
    those arenas badly enough that RSS only ever ratchets upward. Measured on
    this harvest: a third of the growth came back on the first `malloc_trim`.

    Cheap enough to call between batches, a no-op anywhere without glibc.
    """
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6").malloc_trim(0)


def available_mb() -> int:
    """Free memory the kernel believes is actually claimable, in MB.

    MemAvailable, not MemFree: on a box that has been up for a day almost all
    of MemFree has been handed to the page cache, and MemFree alone reads as
    catastrophic while the machine is perfectly healthy.
    """
    try:
        with open(MEMINFO, encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 1 << 30      # unknown (non-Linux): never block on it


def wait_for_memory(
    floor_mb: int = DEFAULT_FLOOR_MB,
    timeout_s: float = 120.0,
    poll_s: float = 2.0,
    on_wait=None,
) -> bool:
    """Block until `floor_mb` is available. True if it cleared, False on timeout.

    The timeout matters: if memory never comes back, waiting forever converts a
    crash into a hang, which is worse — the caller can at least record a skip
    and move on. Returning False is a fact to log, not an error to raise.
    """
    deadline = time.time() + timeout_s
    waited = False
    while available_mb() < floor_mb:
        if time.time() >= deadline:
            return False
        if on_wait and not waited:
            on_wait(available_mb())
            waited = True
        time.sleep(poll_s)
    return True


class Throttle:
    """A named semaphore for a memory-hungry stage.

    Sized by worst case, not average: the cost of a slot is the largest
    document the stage will accept, because that is what decides whether the
    machine survives a bad minute.
    """

    def __init__(self, name: str, slots: int):
        self.name = name
        self.slots = slots
        self._sem = threading.Semaphore(slots)

    def __enter__(self) -> Throttle:
        self._sem.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self._sem.release()
