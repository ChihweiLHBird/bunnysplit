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
# Spelled out on purpose: MicroPython's re accepts "{n}" but never matches it.
_LONG_NUMBER = re.compile("[0-9]" * (MAX_NUMBER_DIGITS + 1))


def check_backup_size(size):
    # Negative means a multi-GiB File.size wrapped: MicroPython's jsffi
    # truncates JS numbers to int32.
    if size < 0 or size > MAX_BACKUP_BYTES:
        raise ValueError("That file is too large to be a bunnysplit backup.")


def check_export_size(text):
    # UTF-8 bytes, the unit of the exported file and of MAX_BACKUP_BYTES.
    if len(text.encode()) > MAX_BACKUP_BYTES:
        raise ValueError("This bill is too large to back up (limit %d KiB)."
                         % (MAX_BACKUP_BYTES // 1024))


def check_restorable(state):
    """Return the export text, or raise ValueError saying why
    parse_backup() would refuse it."""
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
    check_export_size(text)
    return text


def _has_long_number(text):
    # MicroPython converts a huge integer literal in quadratic time (150k
    # digits took 1.5 s), so long digit runs must be refused before json.loads.
    # Only digits outside strings become numbers. Dropping escaped backslashes,
    # then escaped quotes, leaves every remaining '"' as a string delimiter, so
    # the even-indexed pieces of a split lie outside strings. These are C-level
    # string ops; a per-character Python loop is ~20x slower here.
    bare = text.replace("\\\\", "").replace('\\"', "")
    return _LONG_NUMBER.search(" ".join(bare.split('"')[::2])) is not None


def _check_backup_counts(raw):
    # Count the raw lists before building anything. This is conservative on
    # purpose: from_dict() may still drop duplicate or malformed records.
    if len(raw["people"]) > MAX_PEOPLE:
        raise ValueError("A backup can have at most %d people." % MAX_PEOPLE)
    if len(raw["items"]) > MAX_ITEMS:
        raise ValueError("A backup can have at most %d items." % MAX_ITEMS)
    refs = 0
    for item in raw["items"]:
        if isinstance(item, dict) and isinstance(item.get("participant_ids"), list):
            refs += len(item["participant_ids"])
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
    check_backup_size(len(text))
    if _has_long_number(text):
        raise ValueError(
            "That file has a number too long to be a bunnysplit backup.")
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
    state = AppState.from_dict(
        raw, on_issue=lambda kind, message: issues.append(kind + ": " + message))
    # Text lengths and the re-export size can only be judged after from_dict()
    # (it str()s non-string names, and quotes integer ids).
    check_restorable(state)
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
