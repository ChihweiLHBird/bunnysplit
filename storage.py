"""localStorage persistence. Browser-only (imports pyscript)."""

import json
import re

from pyscript import window

from splitcore.model import (
    MAX_DESCRIPTION_LENGTH, MAX_ID_LENGTH, MAX_ITEMS, MAX_NAME_LENGTH,
    MAX_PARTICIPANT_REFS, MAX_PEOPLE, AppState)

KEY = "bunnysplit"
CORRUPT_KEY = KEY + ":corrupt"
_recovery_warning = ""


def _preserve_corrupt(raw, existing_backup):
    # Keep every distinct recovery artifact. The stable base key preserves the
    # first payload; later payloads use numeric suffixes so neither is lost.
    try:
        if existing_backup is None:
            backup_key = CORRUPT_KEY
        elif existing_backup == raw:
            return CORRUPT_KEY
        else:
            suffix = 1
            while True:
                backup_key = CORRUPT_KEY + ":" + str(suffix)
                existing = window.localStorage.getItem(backup_key)
                if existing is None:
                    break
                if existing == raw:
                    return backup_key
                suffix += 1
        window.localStorage.setItem(backup_key, raw)
        return backup_key
    except Exception:
        return None


def recovery_warning():
    return _recovery_warning


def load():
    global _recovery_warning
    _recovery_warning = ""
    # localStorage access itself can throw (Safari private mode or
    # third-party-storage blocked → SecurityError). Treat as "no saved
    # data" rather than letting it propagate and brick page boot.
    try:
        raw = window.localStorage.getItem(KEY)
    except Exception as e:
        window.console.warn("bunnysplit: localStorage unavailable: " + str(e))
        return AppState()
    try:
        existing_backup = window.localStorage.getItem(CORRUPT_KEY)
    except Exception:
        existing_backup = None
    if raw is None:
        if existing_backup is not None:
            _recovery_warning = (
                "A corrupt-data backup is preserved in localStorage as "
                + CORRUPT_KEY + "."
            )
        return AppState()
    try:
        issues = []
        state = AppState.from_dict(
            json.loads(raw),
            on_issue=lambda kind, message: issues.append(kind + ": " + message),
        )
        if issues:
            backup_key = _preserve_corrupt(raw, existing_backup)
            _recovery_warning = "Recovered malformed saved data. "
            if backup_key is not None:
                _recovery_warning += (
                    "A backup is preserved in localStorage as "
                    + backup_key + "."
                )
            else:
                _recovery_warning += "The recovery backup could not be written."
        elif existing_backup is not None:
            _recovery_warning = (
                "A corrupt-data backup is preserved in localStorage as "
                + CORRUPT_KEY + "."
            )
        return state
    except Exception as e:  # corrupt data: surface it, don't silently mask
        window.console.warn("bunnysplit: ignoring corrupt saved state: " + str(e))
        # Stash the bad blob so the next save() doesn't destroy data the
        # user might still recover by hand.
        backup_key = _preserve_corrupt(raw, existing_backup)
        _recovery_warning = "Recovered corrupt saved data. "
        if backup_key is not None:
            _recovery_warning += (
                "A backup is preserved in localStorage as "
                + backup_key + "."
            )
        else:
            _recovery_warning += "The recovery backup could not be written."
        return AppState()


def dumps(state):
    """Serialize state for localStorage and exported backups alike.

    Compact on purpose: MicroPython's json.dumps has no indent kwarg.
    """
    return json.dumps(state.to_dict())


# Both directions are quadratic in size under MicroPython 1.24: json.loads of
# one long string (1 MiB took ~1.25 s) and json.dumps of the whole bill (298
# KiB ~130 ms, 650 KiB ~600 ms, 1.28 MiB ~3 s), and save() runs dumps on every
# edit. 512 KiB keeps both well under a second. The add handlers and import
# both check the exact export size, so every saved bill fits in a backup.
MAX_BACKUP_BYTES = 512 * 1024

# Integer ids may be up to MAX_ID_LENGTH digits; nothing legitimate is longer.
MAX_NUMBER_DIGITS = MAX_ID_LENGTH
# MicroPython 1.24's str hash is 16 bits and its dicts probe linearly, so
# json.loads builds an object whose keys share a hash in quadratic time: one
# of 47,000 keys (470 KiB) took ~30 s, and 512 KiB of such objects with 256
# members each take ~0.4 s. A backup's largest object is an uneven split's
# weights, one per participant (at most MAX_PEOPLE).
MAX_OBJECT_MEMBERS = 256
# A bill at the limits has 3 + MAX_PEOPLE + 4 * MAX_ITEMS objects and lists.
# Counting members walks their brackets in Python, ~2 us apiece under
# MicroPython: 512 KiB of brackets would take ~1 s where json.loads needs
# ~40 ms, so their number is capped (the walk then takes at most ~40 ms).
MAX_CONTAINERS = 2 * (MAX_PEOPLE + 4 * MAX_ITEMS)
# Spelled out on purpose: MicroPython's re accepts "{n}" but never matches it.
_LONG_NUMBER = re.compile("[0-9]" * (MAX_NUMBER_DIGITS + 1))
_HEX = "[0-9a-fA-F]"
_UNICODE_ESCAPE = re.compile("\\\\u" + _HEX * 4)
# Escaped UTF-16 surrogates: a high+low pair, and any half left over.
_SURROGATE_PAIR = re.compile(
    "\\\\u[dD][89abAB]" + _HEX * 2 + "\\\\u[dD][c-fC-F]" + _HEX * 2)
_SURROGATE = re.compile("\\\\u[dD][89a-fA-F]" + _HEX * 2)
# One character, no repetition: MicroPython's re recurses per character on
# "*" and "+", and a run of ~10,000 overflows the JS stack.
_NOT_STRUCTURE = re.compile('[^"{}\\[\\]]')


def check_backup_size(size):
    # UTF-8 bytes: File.size before reading, the decoded text after.
    # Negative means a multi-GiB File.size wrapped: MicroPython's jsffi
    # truncates JS numbers to int32.
    if size < 0 or size > MAX_BACKUP_BYTES:
        raise ValueError("That file is too large to be a bunnysplit backup.")


def check_export_size(text, too_large=None):
    # UTF-8 bytes, the unit of the exported file and of MAX_BACKUP_BYTES.
    # too_large: the message, given the size (rounded up) and the limit in
    # KiB, for callers where "too large to back up" would be wrong.
    size = len(text.encode())
    if size > MAX_BACKUP_BYTES:
        limit = MAX_BACKUP_BYTES // 1024
        if too_large is None:
            raise ValueError(
                "This bill is too large to back up (limit %d KiB)." % limit)
        raise ValueError(too_large % ((size + 1023) // 1024, limit))


def check_restorable(state, too_large=None):
    """Return the export text, or raise ValueError saying why
    parse_backup() would refuse it (see check_export_size())."""
    if len(state.people) > MAX_PEOPLE:
        raise ValueError("A bill can have at most %d people." % MAX_PEOPLE)
    if len(state.items) > MAX_ITEMS:
        raise ValueError("A bill can have at most %d items." % MAX_ITEMS)
    if sum(len(i.participant_ids) for i in state.items) > MAX_PARTICIPANT_REFS:
        raise ValueError(
            "A bill can have at most %d participant entries across all items."
            % MAX_PARTICIPANT_REFS)
    for person in state.people:
        if len(person.name) > MAX_NAME_LENGTH:
            raise ValueError(
                "Names can be at most %d characters." % MAX_NAME_LENGTH)
    for item in state.items:
        if len(item.description) > MAX_DESCRIPTION_LENGTH:
            raise ValueError("Descriptions can be at most %d characters."
                             % MAX_DESCRIPTION_LENGTH)
    text = dumps(state)
    check_export_size(text, too_large)
    return text


def _has_bad_unicode_escape(text):
    # MicroPython's json.loads reads the four characters after \u as hex
    # whatever they are, even a '"', which would hide a string delimiter
    # from _has_long_number(). Standard JSON requires four hex digits.
    # Without escaped backslashes, every remaining backslash starts an escape.
    bare = text.replace("\\\\", "")
    return "\\u" in bare and "\\u" in _UNICODE_ESCAPE.sub("", bare)


def _outside_strings(text):
    # The text with each string replaced by a single '"'. Dropping escaped
    # backslashes, then escaped quotes, leaves every remaining '"' as a
    # string delimiter (given _has_bad_unicode_escape() passed), so the
    # even-indexed pieces of a split lie outside strings. These are C-level
    # string ops; a per-character Python loop is ~20x slower here.
    bare = text.replace("\\\\", "").replace('\\"', "")
    return '"'.join(bare.split('"')[::2])


def _has_long_number(outside):
    # MicroPython converts a huge integer literal in quadratic time (150k
    # digits took 1.5 s), so long digit runs must be refused before json.loads.
    # Only digits outside strings become numbers.
    return _LONG_NUMBER.search(outside) is not None


def _check_structure(outside):
    """Refuse objects with more than MAX_OBJECT_MEMBERS members before
    json.loads builds them (see MAX_OBJECT_MEMBERS).

    MicroPython's json.loads reads ',' and ':' as whitespace, lets ']' close
    '{', takes any value as a key and needs no separator after a string or
    literal, so members can't be counted by colons: values are counted
    instead, two per member. First every value becomes one '"'.
    """
    s = outside
    for word in ("true", "false", "null"):
        s = s.replace(word, '"')
    # A number is a run of these, so map them all to "0" and shorten runs.
    for ch in "-+.123456789eE":
        s = s.replace(ch, "0")
    while "00" in s:
        s = s.replace("00", "0")
    s = s.replace("0", '"')
    for ch in " \t\n\r,:":
        s = s.replace(ch, "")
    # json.loads would fail on anything left, but only after building what
    # comes before it.
    if _NOT_STRUCTURE.search(s) is not None:
        raise ValueError("That file isn't valid JSON.")
    brackets = s.replace('"', "")
    if len(brackets) > 2 * MAX_CONTAINERS:
        raise ValueError(
            "That file has too many objects and lists to be a bunnysplit "
            "backup.")
    # runs[k]: the values between the bracket before brackets[k] and it.
    runs = s.replace("[", "{").replace("]", "{").replace("}", "{").split("{")
    limit = 2 * MAX_OBJECT_MEMBERS
    stack = []  # (values so far, is an object) of each enclosing container
    count = 0
    is_object = False
    k = 0
    # The extra closer at the end also checks containers left open, which
    # json.loads accepts.
    for c in brackets + "}":
        count += len(runs[k])
        k += 1
        opens = c == "{" or c == "["
        if opens:
            count += 1  # the new container is a value of this one
        if is_object and count > limit:
            raise ValueError(
                "That file has an object with too many fields to be a "
                "bunnysplit backup.")
        if opens:
            stack.append((count, is_object))
            count = 0
            is_object = c == "{"
        elif not stack:
            return  # json.loads stops at a closer with nothing open
        else:
            count, is_object = stack.pop()
            if not stack:
                return  # the top-level value is complete


def _surrogate_pair(match):
    s = match.group(0)
    return chr(0x10000 + ((int(s[2:6], 16) - 0xD800) << 10)
               + int(s[8:12], 16) - 0xDC00)


def _decode_surrogate_escapes(text):
    """Return (text, whether an unpaired surrogate escape was replaced).

    MicroPython's json.loads decodes each half of an escaped surrogate pair
    (CPython's json.dumps writes emoji that way by default) on its own, into
    an invalid lone surrogate that the browser shows and saves as U+FFFD
    garbage. So decode pairs to the real character, and unpaired halves to
    U+FFFD, before parsing. Splitting on escaped backslashes keeps a literal
    backslash followed by "ud83d" as it is. The replacements are raw
    characters: MicroPython's re.sub processes backslashes even in what a
    replacement function returns.
    """
    if "\\ud" not in text and "\\uD" not in text:
        return text, False
    parts = text.split("\\\\")
    lone = False
    for k in range(len(parts)):
        part = _SURROGATE_PAIR.sub(_surrogate_pair, parts[k])
        if _SURROGATE.search(part) is not None:
            part = _SURROGATE.sub("\ufffd", part)
            lone = True
        parts[k] = part
    return "\\\\".join(parts), lone


def _check_backup_counts(raw):
    # Count the raw lists before building anything. This is conservative on
    # purpose: from_dict() may still drop duplicate or malformed records.
    if len(raw["people"]) > MAX_PEOPLE:
        raise ValueError("A backup can have at most %d people." % MAX_PEOPLE)
    if len(raw["items"]) > MAX_ITEMS:
        raise ValueError("A backup can have at most %d items." % MAX_ITEMS)
    # No item can have more participants or weights than the roster has
    # people. Item.from_dict()'s dedup set is quadratic in one item's ids
    # when they share a MicroPython hash (see MAX_OBJECT_MEMBERS).
    refs = 0
    for item in raw["items"]:
        if not isinstance(item, dict):
            continue
        pids = item.get("participant_ids")
        if isinstance(pids, list):
            if len(pids) > MAX_PEOPLE:
                raise ValueError(
                    "A backup item can have at most %d participants."
                    % MAX_PEOPLE)
            refs += len(pids)
        split = item.get("split")
        if (isinstance(split, dict) and isinstance(split.get("weights"), dict)
                and len(split["weights"]) > MAX_PEOPLE):
            raise ValueError(
                "A backup item can have at most %d split weights."
                % MAX_PEOPLE)
    if refs > MAX_PARTICIPANT_REFS:
        raise ValueError(
            "A backup can have at most %d participant entries across all "
            "items." % MAX_PARTICIPANT_REFS)


def parse_backup(text):
    """Parse an exported backup into (AppState, issues).

    Raises ValueError with a user-facing message for anything that is not a
    backup. AppState.from_dict() reads a missing people/items key as an empty
    list, so shape is checked here first; otherwise an unrelated JSON object
    would silently import as an empty bill.
    """
    # Encoded, because Blob.text() turns each invalid byte of the file into a
    # three-byte U+FFFD.
    check_backup_size(len(text.encode()))
    if _has_bad_unicode_escape(text):
        raise ValueError("That file isn't valid JSON.")
    outside = _outside_strings(text)
    if _has_long_number(outside):
        raise ValueError(
            "That file has a number too long to be a bunnysplit backup.")
    _check_structure(outside)
    text, lone_surrogates = _decode_surrogate_escapes(text)
    try:
        raw = json.loads(text)
    except Exception:
        raise ValueError("That file isn't valid JSON.")
    if (not isinstance(raw, dict)
            or not isinstance(raw.get("people"), list)
            or not isinstance(raw.get("items"), list)):
        raise ValueError(
            "That file isn't a bunnysplit backup "
            "(expected \"people\" and \"items\" lists).")
    _check_backup_counts(raw)
    issues = []
    if lone_surrogates:
        issue = "text: replaced unpaired surrogate escapes with U+FFFD"
        window.console.warn("bunnysplit: " + issue)
        issues.append(issue)
    state = AppState.from_dict(
        raw, report_caps=True,
        on_issue=lambda kind, message: issues.append(kind + ": " + message))
    # Text lengths and the re-export size can only be judged after from_dict()
    # (it str()s scalar names, and quotes integer ids). The re-export is in
    # the app's format, which can be larger than a minified file.
    check_restorable(
        state, "That backup would be %d KiB once restored, over the %d KiB "
        "limit.")
    return state, issues


def save(state, text=None):
    # text: dumps(state) when the caller already has it (dumps is costly).
    window.localStorage.setItem(KEY, dumps(state) if text is None else text)


def writable():
    # getItem can succeed while setItem throws (Safari private mode has a
    # 0 quota), so probe an actual write to know if persistence works.
    try:
        probe = KEY + ":probe"
        window.localStorage.setItem(probe, "1")
        window.localStorage.removeItem(probe)
        return True
    except Exception:
        return False
