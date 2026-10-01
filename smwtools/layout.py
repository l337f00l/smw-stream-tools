"""Fitting the hack name to your overlay, and keeping the exits under it.

Ported from the OBS script version. The logic is the same; the difference is
that OBS used to report a text source's rendered width one frame after the text
changed, so the old code stepped down on a frame timer. Here each step is a
websocket round trip instead, so it sleeps briefly between setting and
measuring. It only runs when the hack changes, so the extra second is invisible.

Measuring through OBS rather than computing text width locally is deliberate:
it needs no font library, no access to the font file, and it is exactly right
for whatever face, style and scale the source actually uses.
"""

import time

from .kaizoff import trim_subtitle, wrap_two_lines
from .obsws import ObsError, ObsRequestError

# libobs alignment bits. obs-websocket reports the same values.
ALIGN_LEFT = 1
ALIGN_RIGHT = 2
ALIGN_TOP = 4
ALIGN_BOTTOM = 8

SETTLE_S = 0.09     # long enough for OBS to re-render and report a new width


def fit_candidates(text, auto_shorten):
    """Ordered fallbacks, widest first, tried before shrinking the font."""
    candidates = [text]
    if auto_shorten:
        trimmed = trim_subtitle(text)
        if trimmed != text:
            candidates.append(trimmed)
    return candidates


def fit_name(obs, config, text, log=None):
    """Write `text` to the name source, shrinking until it fits.

    In order: try a shorter form of the name, then step the font size down,
    then wrap to two lines at full size. Returns what was finally written.
    """
    log = log or (lambda *_: None)
    source = config["name_source"]
    if not source or not text:
        return text

    max_width = config["max_width"]
    base_font = config["base_font"]
    min_font = config["min_font"]

    scene, item_id = obs.find_item(source)
    font = obs.get_font(source)
    if scene is None or not font:
        # Not in the current scene, or not a font-bearing source. Write the
        # text plainly rather than failing.
        obs.set_text(source, text)
        return text

    candidates = fit_candidates(text, config["auto_shorten"])
    index = 0
    current = candidates[0]
    size = base_font
    wrapped = False

    obs.set_text(source, current)
    obs.set_font_size(source, size, font)

    for _ in range(40):          # generous ceiling; the loop always converges
        time.sleep(SETTLE_S)
        try:
            width, _height, _t = obs.source_size(scene, item_id)
        except ObsRequestError:
            return current
        if width <= 0:
            continue             # not rendered yet
        if width <= max_width:
            return current

        # 1. A shorter form at full size beats the whole title squinting.
        if index + 1 < len(candidates):
            index += 1
            current = candidates[index]
            size = base_font
            obs.set_text(source, current)
            obs.set_font_size(source, size, font)
            continue

        # 2. Shrink.
        if size > min_font:
            size = max(min_font, size - 2)
            obs.set_font_size(source, size, font)
            continue

        # 3. Bottomed out — wrap to two lines and start over at full size.
        if not wrapped:
            wrapped = True
            current = wrap_two_lines(current)
            size = base_font
            obs.set_text(source, current)
            obs.set_font_size(source, size, font)
            continue

        break                    # as small and as wrapped as it gets

    log("name fitted at %dpt%s" % (size, " (wrapped)" if wrapped else ""))
    return current


def _edges(transform):
    """Top-left of an item in scene space.

    OBS positions a scene item at its *alignment anchor*, not always its
    top-left: a right-aligned item reports its right edge, a centred one its
    centre. Resolving that here is what makes this work for left-side,
    right-side and centred overlays alike.
    """
    align = int(transform.get("alignment") or 0)
    width = (transform.get("sourceWidth") or 0) * (transform.get("scaleX") or 1.0)
    height = (transform.get("sourceHeight") or 0) * (transform.get("scaleY") or 1.0)
    x = transform.get("positionX") or 0.0
    y = transform.get("positionY") or 0.0

    if align & ALIGN_RIGHT:
        left = x - width
    elif align & ALIGN_LEFT:
        left = x
    else:
        left = x - width / 2.0

    if align & ALIGN_BOTTOM:
        top = y - height
    elif align & ALIGN_TOP:
        top = y
    else:
        top = y - height / 2.0
    return left, top, width, height, align


def _anchor(align, left, top, width, height):
    """Turn a desired top-left back into the position OBS expects."""
    if align & ALIGN_RIGHT:
        x = left + width
    elif align & ALIGN_LEFT:
        x = left
    else:
        x = left + width / 2.0

    if align & ALIGN_BOTTOM:
        y = top + height
    elif align & ALIGN_TOP:
        y = top
    else:
        y = top + height / 2.0
    return x, y


def reflow_exits(obs, config, log=None):
    """Keep the exits source positioned relative to the hack name.

    'below' — under the name, following it down when a long name wraps.
    'right' — after the name on the same line.
    'off'   — never touch the scene.

    The exits source keeps its own horizontal anchoring: a right-anchored pair
    stays flush right, a centred pair stays centred.
    """
    log = log or (lambda *_: None)
    mode = config["layout_mode"]
    name_source, exits_source = config["name_source"], config["exits_source"]
    if mode == "off" or not name_source or not exits_source:
        return False

    try:
        scene, name_id = obs.find_item(name_source)
        if scene is None:
            return False
        exits_scene, exits_id = obs.find_item(exits_source, scene)
        if exits_id is None:
            # The two can live in different groups; look wider before giving up.
            exits_scene, exits_id = obs.find_item(exits_source)
            if exits_id is None:
                return False

        n_left, n_top, n_width, n_height, _ = _edges(obs.get_transform(scene, name_id))
        e_transform = obs.get_transform(exits_scene, exits_id)
        e_left, e_top, e_width, e_height, e_align = _edges(e_transform)
        if n_width <= 0 or n_height <= 0:
            return False

        if mode == "right":
            target_left = n_left + n_width + config["gap"]
            target_top = e_top
        else:
            if e_align & ALIGN_RIGHT:
                target_left = (n_left + n_width) - e_width
            elif e_align & ALIGN_LEFT:
                target_left = n_left
            else:
                target_left = n_left + (n_width - e_width) / 2.0
            target_top = n_top + n_height + config["gap"]

        x, y = _anchor(e_align, target_left, target_top, e_width, e_height)
        obs.set_position(exits_scene, exits_id, x, y)
        return True
    except ObsRequestError as exc:
        log("could not reflow exits: %s" % exc)
        return False
    except ObsError:
        raise
