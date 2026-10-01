"""Twitch stream title -> kaizoff.com hack record.

Ported from the original kaizoff_obs.py OBS script. The matching rules are
unchanged — they were tuned against real stream titles and the failure modes
they avoid are specific and hard-won. What changed is that nothing here touches
OBS any more; this module answers "which hack is being streamed" and the caller
decides what to draw.
"""

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

HACKS_URL = "https://kaizoff.com/api/public/v1/hacks/index"
HACK_DETAIL_URL = "https://kaizoff.com/api/public/v1/hacks/%s"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"
USER_AGENT = "smw-stream-tools/1.0"

# Real hack names that are ordinary English words — auto-matching these off a
# stream title causes more false hits than it's worth. Use the "match" section
# of overrides.json if you genuinely want to stream one of them.
GENERIC_NAMES = {
    "hack", "hack 2", "hack 3", "hack 4", "hack 5", "mario", "title",
    "collection", "collab", "colors", "the level", "stranded", "terraria",
    "shipwrecked", "underworld", "electra", "ten", "square", "dumb", "boris",
}

# Roman numerals converted wherever they appear as a whole token, so
# "Tortured Souls II" lines up with "Tortured Souls 2". Hack names and titles
# both run through this, so the substitution is symmetric. Bare I, V and X are
# excluded: they'd wreck names ending in a letter, e.g. "Super Mario X".
ROMAN_TOKENS = {
    "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7",
    "viii": "8", "ix": "9", "xi": "11", "xii": "12",
}


def normalize(text):
    """Lowercase, strip punctuation, collapse whitespace — for matching only."""
    text = (text or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return " ".join(ROMAN_TOKENS.get(tok, tok) for tok in text.split())


def trim_subtitle(text):
    """'Tortured Souls II: Rapture' -> 'Tortured Souls II'.

    Only splits on a separator with real content on both sides, so names like
    'Kaizo: The Hack' keep their head and ':)' style names are untouched.
    """
    for sep in (":", " - ", " – ", " — "):
        head, found, tail = text.partition(sep)
        if found and len(head.strip()) >= 4 and tail.strip():
            return head.strip()
    return text


def wrap_two_lines(text):
    """Split on the word boundary closest to the middle."""
    words = text.split()
    if len(words) < 2:
        return text
    target = len(text) / 2
    best_idx, best_delta = 1, None
    for idx in range(1, len(words)):
        delta = abs(len(" ".join(words[:idx])) - target)
        if best_delta is None or delta < best_delta:
            best_idx, best_delta = idx, delta
    return " ".join(words[:best_idx]) + "\n" + " ".join(words[best_idx:])


def match_hack(title, hacks, overrides=None, log=None):
    """Find the hack referenced in a stream title.

    In order:
      1. A manual override keyword.
      2. The LONGEST API hack name appearing verbatim in the title, so
         "Super Mario World" doesn't beat "Super Mario World 2".
      3. Fuzzy match against the whole title, above a confidence floor.

    Returns (hack, reason). `hack` is None when nothing matched or when the
    sequel guard refused; `reason` explains which, for the UI.
    """
    overrides = overrides or {}
    log = log or (lambda *_: None)
    norm_title = normalize(title)
    if not norm_title:
        return None, "empty title"

    for keyword, target in (overrides.get("match") or {}).items():
        if normalize(keyword) and normalize(keyword) in norm_title:
            for hack in hacks:
                if hack.get("name") == target:
                    return hack, "override %r" % keyword

    best, best_len = None, 0
    for hack in hacks:
        norm_name = normalize(hack.get("name", ""))
        if len(norm_name) < 4 or norm_name in GENERIC_NAMES:
            continue
        # \b so "Hack" doesn't match inside "romhack", "EP" inside "epic".
        if re.search(r"\b%s\b" % re.escape(norm_name), norm_title):
            if len(norm_name) > best_len:
                best, best_len = hack, len(norm_name)

    if best:
        # Sequel guard: if the title continues with a number right after the
        # name we matched, we've probably grabbed the base game while they're
        # playing the sequel. A wrong exit count is worse than none.
        norm_best = normalize(best["name"])
        trailing = re.search(r"\b%s\s+(\d+)\b" % re.escape(norm_best), norm_title)
        if trailing:
            siblings = sorted(
                h["name"] for h in hacks
                if normalize(h.get("name", "")).startswith(norm_best))[:8]
            log("refusing to match %r — title continues with %r, so this looks "
                "like a sequel the index doesn't have under that name"
                % (best["name"], trailing.group(1)))
            if siblings:
                log("  index entries starting with %r: %s"
                    % (best["name"], ", ".join(repr(s) for s in siblings)))
            return None, ("looks like a sequel of %r — add the right name to "
                          "overrides" % best["name"])
        return best, "exact"

    best, best_score = None, 0.0
    for hack in hacks:
        norm_name = normalize(hack.get("name", ""))
        if len(norm_name) < 5:
            continue
        score = SequenceMatcher(None, norm_name, norm_title).ratio()
        if score > best_score:
            best, best_score = hack, score
    if best and best_score >= 0.62:
        return best, "fuzzy %.2f" % best_score

    return None, "no match in title"


class Kaizoff(object):
    """Hack index, author lookups and the Twitch title fetch."""

    def __init__(self, data_dir, log=None):
        self.data_dir = data_dir
        self.log = log or (lambda *_: None)
        self.cache_file = os.path.join(data_dir, "hacks_cache.json")
        self.authors_file = os.path.join(data_dir, "authors_cache.json")
        self.overrides_file = os.path.join(data_dir, "overrides.json")
        self.progress_file = os.path.join(data_dir, "progress.json")

        self.hacks = []
        self.authors = self._load(self.authors_file, {}) or {}
        self.progress = self._load(self.progress_file, {}) or {}
        self.overrides = self._load(self.overrides_file, None) or {}
        self.overrides.setdefault("display", {})
        self.overrides.setdefault("match", {})

        self._token = {"value": None, "expires": 0}
        self._broadcaster_id = None

        os.makedirs(data_dir, exist_ok=True)
        if not os.path.exists(self.overrides_file):
            self._save(self.overrides_file, {
                "_comment": ("display: force a short form on stream. "
                             "match: map a keyword in your title to an exact "
                             "API hack name."),
                "display": {"Tortured Souls 2": "TS2"},
                "match": {"ts2": "Tortured Souls 2"},
            })
        # Cache only — no network at startup.
        self.hacks = (self._load(self.cache_file, None) or {}).get("hacks", [])

    # -- disk ------------------------------------------------------------

    def _load(self, path, default):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return default

    def _save(self, path, payload):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
        except Exception as exc:
            self.log("could not write %s: %s" % (path, exc))

    # -- http ------------------------------------------------------------

    def _json(self, url, data=None, headers=None, timeout=15, context=None):
        request = urllib.request.Request(
            url, data=data, headers=dict(headers or {}, **{"User-Agent": USER_AGENT}))
        with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _kaizoff_get(self, url, allow_insecure, timeout=30):
        try:
            return self._json(url, timeout=timeout)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLCertVerificationError) and allow_insecure:
                # Public, read-only data. Twitch calls carry a secret and are
                # NEVER downgraded this way.
                self.log("cert verify failed; retrying unverified "
                         "(public data only — Twitch calls stay verified)")
                return self._json(url, timeout=timeout,
                                  context=ssl._create_unverified_context())
            raise

    # -- hack index ------------------------------------------------------

    def fetch_hacks(self, cache_hours=12, allow_insecure=False, force=False):
        cached = self._load(self.cache_file, None)
        if cached and not force:
            if time.time() - cached.get("fetched_at", 0) < cache_hours * 3600:
                self.hacks = cached.get("hacks", [])
                return self.hacks
        try:
            payload = self._kaizoff_get(HACKS_URL, allow_insecure)
        except Exception as exc:
            self.log("hack index fetch failed (%s), using cache" % exc)
            self.hacks = (cached or {}).get("hacks", []) or self.hacks
            return self.hacks
        hacks = payload.get("data", payload if isinstance(payload, list) else [])
        self._save(self.cache_file, {"fetched_at": time.time(), "hacks": hacks})
        self.hacks = hacks
        self.log("fetched %d hacks" % len(hacks))
        return hacks

    def fetch_authors(self, hack_id, allow_insecure=False):
        """Authors come from a second endpoint, so this is cached forever."""
        key = str(hack_id)
        if key in self.authors:
            return self.authors[key]
        try:
            payload = self._kaizoff_get(HACK_DETAIL_URL % key, allow_insecure, timeout=20)
        except Exception as exc:
            self.log("author lookup failed for hack %s (%s)" % (key, exc))
            return ""
        data = payload.get("data", payload) or {}
        names = [a.get("name", "") for a in (data.get("authors") or []) if a.get("name")]
        joined = ", ".join(names)
        self.authors[key] = joined
        self._save(self.authors_file, self.authors)
        return joined

    # -- twitch ----------------------------------------------------------

    def _token_value(self, client_id, client_secret):
        if self._token["value"] and time.time() < self._token["expires"] - 60:
            return self._token["value"]
        body = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }).encode()
        payload = self._json(TOKEN_URL, data=body)
        self._token["value"] = payload["access_token"]
        self._token["expires"] = time.time() + payload.get("expires_in", 3600)
        return self._token["value"]

    def stream_title(self, channel, client_id, client_secret):
        headers = {
            "Client-Id": client_id,
            "Authorization": "Bearer %s" % self._token_value(client_id, client_secret),
        }
        if not self._broadcaster_id:
            url = "%s/users?login=%s" % (HELIX, urllib.parse.quote(channel))
            entries = self._json(url, headers=headers).get("data", [])
            if not entries:
                raise RuntimeError("no such Twitch channel: %s" % channel)
            self._broadcaster_id = entries[0]["id"]
        url = "%s/channels?broadcaster_id=%s" % (HELIX, self._broadcaster_id)
        entries = self._json(url, headers=headers).get("data", [])
        return entries[0].get("title", "") if entries else ""

    def forget_channel(self):
        self._broadcaster_id = None
        self._token = {"value": None, "expires": 0}

    # -- display ---------------------------------------------------------

    def display_name(self, hack):
        """Honour a manual short form, e.g. Tortured Souls 2 -> TS2."""
        return (self.overrides.get("display") or {}).get(hack["name"], hack["name"])

    def set_progress(self, hack_name, value):
        self.progress[hack_name] = value
        self._save(self.progress_file, self.progress)

    def get_progress(self, hack_name):
        return int(self.progress.get(hack_name, 0))
