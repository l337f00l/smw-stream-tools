"""Memory scan tools — for hacks whose addresses don't match the defaults.

The app ships with addresses that were found and confirmed on hardware, and
they hold for the great majority of hacks, because most are patches over
vanilla Super Mario World and leave its variables where they were. These tools
are for the ones that aren't: a hack with a custom retry patch, a
non-SMW-based ROM, or anything where the exit count sits still while you play.

The method is the same throughout: sample memory, do the thing you're looking
for, sample again, and keep only the bytes that behaved the way they should
have. Repeat until few enough candidates remain to check by eye. It converges
quickly because a byte has to survive *every* round.

These run in the console rather than the web UI because finding an address is
a conversation — sample, play, come back, sample again — and because you only
ever do it once per hack.
"""

import argparse
import sys
import time

from .config import Config
from .snes import Usb2Snes, WRAM_BASE, FILLER

LOWRAM_SIZE = 0x2000       # $7E:0000-$7E:1FFF — SMW's working variables

COMMANDS = ("devices", "title", "scan", "probe", "watch", "deaths",
            "find-death", "find-death-counter")


# -- shared ---------------------------------------------------------------

def _connect(args):
    """Open a connection using the app's own saved settings as defaults.

    Reusing the config means you don't retype a URL or device filter you have
    already got working in the web UI.
    """
    config = Config()
    url = args.url or config["snes_url"]
    device = args.device if args.device is not None else config["snes_device"]
    snes = Usb2Snes(url, device)
    print("Connecting to %s%s" % (url, " (device %r)" % device if device else ""))
    name = snes.connect()
    print("Connected to: %s\n" % name)
    return snes


def _label(addr):
    """The SNES-side address for an FX Pak address, where there is one.

    Worth printing because everything written about SMW hacking talks in
    $7E:xxxx terms, so this is what lets you look a candidate up.
    """
    if WRAM_BASE <= addr < WRAM_BASE + 0x20000:
        offset = addr - WRAM_BASE
        return "($%02X:%04X)" % (0x7E + (offset >> 16), offset & 0xFFFF)
    if 0xE00000 <= addr < 0xF00000:
        return "(SRAM +%04X)" % (addr - 0xE00000)
    return ""


def _warn_if_filler(snes):
    """Catch the case where memory isn't really being exposed.

    Worth checking first: every scan below would otherwise run to completion
    and report nothing, which looks like "this hack is unusual" rather than
    "nothing was ever readable".
    """
    try:
        # A wide sample, because a short one can be uniform by chance and a
        # false "nothing is exposed" sends you off fixing the wrong thing.
        sample = snes.read(WRAM_BASE, 0x200)
    except Exception:
        return False
    if len(set(sample)) > 1:
        return False
    if sample[0] in (FILLER, 0xFF):
        print("  !! Memory reads as a constant 0x%02X — nothing real is being"
              " exposed.\n" % sample[0])
        print("     On an emulator this is usually the wrong core. The")
        print("     connector reads BizHawk's System Bus domain, which the")
        print("     Snes9x core does not expose — switch to BSNES under")
        print("     Config -> Cores -> SNES, reload the ROM, and restart")
        print("     the connector script.\n")
        print("     On an FXPak Pro it means the ROM uses an enhancement chip")
        print("     the pak cannot mirror. SA-1 hacks land here.\n")
    else:
        print("  !! Memory reads as all 0x%02X. That is usually just a game"
              % sample[0])
        print("     that hasn't started yet — load your save file and try"
              " again.\n")
    return True


def _narrow(snes, args, prompt, keep, describe, nothing_left, done_hint):
    """The loop every scan shares: sample, prompt, sample, intersect.

    `keep` decides which byte indexes survive a round. Intersecting across
    rounds rather than trusting one is the whole trick — a single sample
    always leaves hundreds of bytes that merely happened to differ.
    """
    _warn_if_filler(snes)
    base, size = args.base, args.size
    print("Scanning %06X-%06X\n" % (base, base + size - 1))

    before = snes.read(base, size)
    candidates = None
    round_no = 0
    while True:
        try:
            input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        after = snes.read(base, size)
        hits = keep(before, after, size)
        changed = sum(1 for i in range(size) if after[i] != before[i])
        candidates = hits if candidates is None else (candidates & hits)
        before = after
        round_no += 1

        print("  %d of %d bytes changed at all" % (changed, size))
        print("  after %d round(s): %d candidate(s)" % (round_no, len(candidates)))
        for i in sorted(candidates)[:15]:
            print("     %06X  %-14s %s" % (base + i, _label(base + i),
                                           describe(after, i)))
        if not candidates:
            print("\n" + nothing_left)
            return
        if len(candidates) <= 3:
            best = base + sorted(candidates)[0]
            print("\n  Narrow enough. Confirm it moves only when it should:")
            print("     python run.py watch %06X" % best)
            print("  " + done_hint % ("%06X" % best))


# -- commands -------------------------------------------------------------

def cmd_devices(args):
    """List what the server can see, hardware and emulators alike."""
    config = Config()
    snes = Usb2Snes(args.url or config["snes_url"], "")
    snes.connect()
    for name in snes.devices:
        print("  ", name)
    print("\nPut part of a name in Device filter to pin one, e.g. 'fxpak'.")
    print("Emulators appear as luabridge://... rather than by name.")


def cmd_title(args):
    """Print the ROM identity — the key the app files your counts under."""
    snes = _connect(args)
    rom_id = snes.read_rom_id(args.address)
    if not rom_id:
        print("No valid ROM header at %06X." % args.address)
        print("Try 00FFC0 for a HiROM hack, or clear the ROM address setting.")
        return
    print("ROM id: %r\n" % rom_id)
    print("The name is usually just 'SUPER MARIO WORLD' — hacks rarely change")
    print("it. The bracketed checksum is what actually tells two hacks apart,")
    print("so load a different hack and check that it changes.")


def cmd_scan(args):
    """Find the exit counter: the byte that goes up when you clear a level."""
    snes = _connect(args)
    print("Find the byte that counts exits.\n")
    _narrow(
        snes, args,
        "\nClear a level that SHOULD count as an exit, then press Enter... ",
        lambda before, after, size: {
            i for i in range(size) if after[i] == (before[i] + args.delta) & 0xFF},
        lambda after, i: "= %d" % after[i],
        "Nothing incremented. Check the level actually counted — re-clearing\n"
        "one you have already beaten does not add an exit. A switch palace\n"
        "that the hack does not count is also a normal no-op.",
        "Then put %s in Exit counter address.")


def cmd_find_death(args):
    """Find a byte that holds a distinctive value while you are dying.

    Deaths are not counted by the game, so there is nothing to read directly —
    what there is, is a byte that means "the player is dying". Each round
    samples both alive and dying, and a candidate must hold the same value at
    every death and one it never holds while alive. Re-sampling alive every
    round is what makes it converge.
    """
    snes = _connect(args)
    _warn_if_filler(snes)
    base, size = args.base, args.size
    print("Find the byte that means 'dying'.")
    print("Scanning %06X-%06X\n" % (base, base + size - 1))

    alive_seen = {}
    death_values = {}
    candidates = None
    round_no = 0
    while True:
        try:
            input("\nPlay normally for a few seconds, then press Enter... ")
            alive = snes.read(base, size)
            for i in range(size):
                alive_seen.setdefault(i, set()).add(alive[i])
            input("Now %s, and press Enter WHILE it is on screen... " % args.action)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        dying = snes.read(base, size)
        round_no += 1

        hits = {i for i in range(size) if dying[i] not in alive_seen[i]}
        if candidates is None:
            candidates = hits
            death_values = {i: dying[i] for i in hits}
        else:
            candidates = {i for i in candidates
                          if i in hits and dying[i] == death_values.get(i)}

        print("  after %d round(s): %d candidate(s)" % (round_no, len(candidates)))
        for i in sorted(candidates)[:15]:
            value = death_values[i]
            print("     %06X  %-14s = 0x%02X (%d decimal)"
                  % (base + i, _label(base + i), value, value))

        if not candidates:
            print("\n  Nothing consistent. Press Enter earlier in the death")
            print("  animation — on a quick-retry hack the state can be gone")
            print("  in a couple of frames. If it always is, try")
            print("  find-death-counter instead.")
            return
        if len(candidates) <= 4:
            best = sorted(candidates)[0]
            print("\n  Narrow enough. Confirm it shows that value ONLY when")
            print("  dying — go through a pipe and a door and watch it stay put:")
            print("     python run.py watch %06X" % (base + best))
            print("  Then put %06X in Death state address and 0x%02X in Death"
                  % (base + best, death_values[best]))
            print("  state value — as hex, without the 0x.")


def cmd_find_death_counter(args):
    """Find a byte that goes up by one each time you die.

    Some retry patches keep their own death total. That is sturdier than a
    state byte, because a quick-retry death can be over faster than the link
    can poll — so if find-death keeps coming up empty, look for one of these.
    """
    snes = _connect(args)
    print("Find a cumulative death counter.\n")
    _narrow(
        snes, args,
        "\nDie once, let the retry settle, then press Enter... ",
        lambda before, after, size: {
            i for i in range(size) if after[i] == (before[i] + 1) & 0xFF},
        lambda after, i: "= %d" % after[i],
        "No byte increments consistently, so this hack probably keeps no\n"
        "death total. Use find-death instead.",
        "Then put %s in Death address and set Death detection to\n"
        "  'A total the hack keeps'.")


def cmd_watch(args):
    """Print a byte whenever it changes — the check before you trust anything."""
    snes = _connect(args)
    print("Watching %06X. Ctrl-C to stop.\n" % args.address)
    last = None
    while True:
        value = snes.read_u8(args.address)
        if value != last:
            # Hex first, always. Every address and value field in the settings
            # is hex, and a decimal value pasted into one silently never
            # matches — which is a genuinely horrible afternoon.
            print("%s  %06X = 0x%02X   (%d decimal)"
                  % (time.strftime("%H:%M:%S"), args.address, value, value))
            last = value
        time.sleep(0.15)


def cmd_probe(args):
    """Show game mode and the counter side by side, to choose a gate.

    The gate is what stops the title screen demo — which plays a real level —
    from being read as progress. Move around the game and note which mode
    values appear where.
    """
    snes = _connect(args)
    _warn_if_filler(snes)
    print("Move between title screen, file select, overworld and a level.")
    print("Note the mode where the counter becomes trustworthy. Ctrl-C to stop.\n")
    last = None
    while True:
        mode = snes.read_u8(args.gate)
        count = snes.read_u8(args.address)
        if (mode, count) != last:
            print("  mode %02X   counter %3d" % (mode, count))
            last = (mode, count)
        time.sleep(0.1)


def cmd_deaths(args):
    """Count deaths as fast as the link allows, and time how long each lasts.

    This is the ground truth for death detection. It answers the two questions
    that matter: does this byte reach the death value at all on this hack, and
    does it hold long enough to be seen? If the shortest death is under the
    poll interval, that is exactly why deaths go missing.
    """
    snes = _connect(args)
    print("Watching %06X for 0x%02X every %dms. Ctrl-C to stop.\n"
          % (args.address, args.value, args.interval))
    deaths, errors, shortest = 0, 0, None
    high_since = None
    while True:
        try:
            raw = snes.read_u8(args.address)
            errors = 0
        except Exception as exc:
            errors += 1
            print("  read failed (%d in a row): %s" % (errors, exc))
            if errors >= 5:
                print("\nThe link is failing, not the detection. Check the URL")
                print("is ws://localhost:23074, then restart the server.")
                return
            time.sleep(0.5)
            continue

        now = time.time()
        if raw == args.value:
            if high_since is None:
                high_since, deaths = now, deaths + 1
        elif high_since is not None:
            held = (now - high_since) * 1000.0
            shortest = held if shortest is None else min(shortest, held)
            print("  death %-3d held %6.0f ms   (shortest so far %.0f ms)"
                  % (deaths, held, shortest))
            high_since = None
        time.sleep(args.interval / 1000.0)


# -- entry point ----------------------------------------------------------

def _hex(text):
    return int(text, 16)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run.py", description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    def add(name, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--url", default=None, help="server URL (default: your saved setting)")
        p.add_argument("--device", default=None, help="device filter, e.g. fxpak")
        return p

    def add_region(p):
        p.add_argument("--base", type=_hex, default=WRAM_BASE)
        p.add_argument("--size", type=_hex, default=LOWRAM_SIZE)
        return p

    add("devices", "list the devices the server can see")

    p = add("title", "print the loaded ROM's identity")
    p.add_argument("--address", type=_hex, default=0x007FC0)

    p = add_region(add("scan", "find the exit counter"))
    p.add_argument("--delta", type=int, default=1,
                   help="how much an exit adds (default 1)")

    add_region(add("find-death", "find the byte that means 'dying'"))
    sub.choices["find-death"].add_argument(
        "--action", default="die", help="what to do each round (default: die)")

    add_region(add("find-death-counter", "find a cumulative death counter"))

    p = add("watch", "print a byte whenever it changes")
    p.add_argument("address", type=_hex)

    p = add("probe", "show game mode alongside a counter")
    p.add_argument("address", type=_hex, help="counter address, e.g. F51F2E")
    p.add_argument("--gate", type=_hex, default=0xF50100)

    p = add("deaths", "count deaths and time how long each lasts")
    p.add_argument("--address", type=_hex, default=0xF5009D)
    p.add_argument("--value", type=_hex, default=0x30)
    p.add_argument("--interval", type=int, default=20, help="poll ms")

    return parser


HANDLERS = {
    "devices": cmd_devices, "title": cmd_title, "scan": cmd_scan,
    "probe": cmd_probe, "watch": cmd_watch, "deaths": cmd_deaths,
    "find-death": cmd_find_death, "find-death-counter": cmd_find_death_counter,
}


def main(argv):
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return 1
    try:
        HANDLERS[args.command](args)
    except KeyboardInterrupt:
        print("\nstopped.")
    except Exception as exc:
        print("\n%s" % exc)
        return 1
    return 0
