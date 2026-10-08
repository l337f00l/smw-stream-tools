"""Finding an address from the settings page, one button press at a time.

The command-line tools do this already, but they need a terminal and a Python
install — which is exactly the wall a non-technical user hits on the hack that
needs them most. This is the same method driven by clicking: take a snapshot,
go do the thing, click, take another, and keep only the bytes that behaved the
way they should have. A byte has to survive every round, so it converges fast.

Only the searches that can be driven by a button live here. Finding a byte that
means "dying" needs you to press at the exact moment it is on screen, which on
a quick-retry hack is a couple of frames — that one stays in the CLI, and is
also the search you do not want on such a hack anyway. The counter searches are
what matter here, because a total the hack keeps is what fixes a death state
too brief to catch.
"""

import threading

WRAM_BASE = 0xF50000
# All of WRAM, $7E and $7F. The first 8KB holds vanilla SMW's own variables
# and was the original search area, but a hack with a custom retry patch
# usually keeps its state well outside that — the one that prompted this kept
# its death total at $7F:B424, which an 8KB search could never have found.
REGION_SIZE = 0x20000

MODES = {
    "deaths": {
        "title": "Find the death counter",
        "lead": "Some hacks keep their own death total. A total can't be missed "
                "the way a brief death animation can, so this is the fix when "
                "deaths are being undercounted.",
        "action": "Die once, let the retry finish, then press the button.",
        "button": "I died",
        "applies": {"death_addr": "%06X", "death_mode": "counter"},
        "applied": "Death address set, and detection switched to the counter.",
        "empty": "No byte went up by one each time, so this hack doesn't keep a "
                 "death total. Lower the poll interval instead — 50 catches far "
                 "more than the default 200.",
    },
    "exits": {
        "title": "Find the exit counter",
        "lead": "For a hack whose exit count never moves, because it keeps the "
                "total somewhere other than the usual place.",
        "action": "Clear a level that should count as an exit, then press the "
                  "button.",
        "button": "I cleared a level",
        "applies": {"exit_addr": "%06X"},
        "applied": "Exit counter address set.",
        "empty": "Nothing went up by one. Check the level actually counted — "
                 "re-clearing one you have already beaten adds nothing, and a "
                 "switch palace the hack doesn't count is a normal no-op.",
    },
}

MAX_SHOWN = 8
NARROW_ENOUGH = 3


def label(addr):
    """The SNES-side address, which is how everything written about SMW
    hacking refers to these."""
    if WRAM_BASE <= addr < WRAM_BASE + 0x20000:
        offset = addr - WRAM_BASE
        return "$%02X:%04X" % (0x7E + (offset >> 16), offset & 0xFFFF)
    return ""


class ScanSession(object):
    """One address hunt. Only one runs at a time, which is plenty."""

    def __init__(self, tracker, log=None):
        self.tracker = tracker
        self.log = log or (lambda *_: None)
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.mode = None
            self.round = 0
            self.candidates = None
            self.before = None
            self.values = {}
            self.message = ""
            self.error = ""
            self.done = False
            self.busy = False

    # -- driving ----------------------------------------------------------

    def start(self, mode):
        if mode not in MODES:
            raise ValueError("unknown search %r" % mode)
        self.reset()
        with self._lock:
            self.mode = mode
            self.busy = True
        try:
            before = self.tracker.sample_region(WRAM_BASE, REGION_SIZE)
        except Exception as exc:
            with self._lock:
                self.busy = False
                self.error = str(exc)
            return self.status()
        with self._lock:
            self.before = before
            self.busy = False
            self.message = MODES[mode]["action"]
        self.log("address finder: started %s" % mode)
        return self.status()

    def step(self):
        """One round: sample again and keep only what moved correctly."""
        with self._lock:
            if self.before is None:
                self.error = "start a search first"
                return self.status()
            mode, before = self.mode, self.before
            self.busy = True
            self.error = ""
        try:
            after = self.tracker.sample_region(WRAM_BASE, REGION_SIZE)
        except Exception as exc:
            with self._lock:
                self.busy = False
                self.error = str(exc)
            return self.status()

        # A counter goes up by exactly one. Everything else is noise, and
        # intersecting across rounds is what clears it out — a single sample
        # always leaves hundreds of bytes that merely happened to differ.
        hits = {i for i in range(len(after))
                if after[i] == (before[i] + 1) & 0xFF}
        with self._lock:
            self.candidates = hits if self.candidates is None else (self.candidates & hits)
            self.before = after
            self.round += 1
            self.values = {i: after[i] for i in self.candidates}
            self.busy = False
            count = len(self.candidates)
            if count == 0:
                self.done = True
                self.message = MODES[mode]["empty"]
            elif count <= NARROW_ENOUGH:
                self.done = True
                self.message = ("Narrow enough. Pick the one that looks right — "
                                "if more than one is left, do another round to "
                                "be sure.")
            else:
                self.message = ("%d possibilities left. %s"
                                % (count, MODES[mode]["action"]))
        self.log("address finder: round %d, %d candidate(s)" % (self.round, count))
        return self.status()

    def apply(self, config, addr):
        """Record the chosen address.

        A death address belongs to the hack it was found in, not to the app:
        the next hack will keep its deaths somewhere else, and a counter
        address from one hack is arbitrary memory in another. So it is saved
        against the loaded ROM, and switching hacks switches the setting with
        no action from anyone.
        """
        with self._lock:
            mode = self.mode
        if mode not in MODES:
            raise ValueError("no search is running")

        rom = (self.tracker.state or {}).get("rom")
        if mode == "deaths" and rom:
            self.tracker.set_hack_deaths(rom, "%06X" % addr, "counter")
            with self._lock:
                self.message = ("Saved for this hack. Other hacks keep their "
                                "own settings, so switching is automatic.")
                self.done = True
            return []

        changes = {}
        for key, form in MODES[mode]["applies"].items():
            changes[key] = (form % addr) if "%" in form else form
        config.update(changes)
        self.log("address finder: %s = %06X" % (mode, addr))
        with self._lock:
            self.message = MODES[mode]["applied"]
            self.done = True
        return list(changes.keys())

    # -- reporting --------------------------------------------------------

    def status(self):
        with self._lock:
            found = []
            if self.candidates is not None:
                for offset in sorted(self.candidates)[:MAX_SHOWN]:
                    addr = WRAM_BASE + offset
                    found.append({"addr": addr, "hex": "%06X" % addr,
                                  "label": label(addr),
                                  "value": self.values.get(offset, 0)})
            spec = MODES.get(self.mode or "", {})
            return {
                "running": self.mode is not None,
                "mode": self.mode,
                "title": spec.get("title", ""),
                "lead": spec.get("lead", ""),
                "button": spec.get("button", ""),
                "round": self.round,
                "total": len(self.candidates) if self.candidates is not None else None,
                "candidates": found,
                "more": (len(self.candidates) - len(found)) if self.candidates else 0,
                "message": self.message,
                "error": self.error,
                "busy": self.busy,
                "done": self.done,
            }
