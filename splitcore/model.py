"""Data model for bunnysplit.

Plain classes (no dataclasses) so this runs identically under CPython and
MicroPython. Money is integer cents everywhere; formatting happens in the UI.
"""

import sys

MODE_EQUAL = "equal"
MODE_UNEVEN = "uneven"

# $1B. Caps persisted amounts so the float-based uneven split stays exact:
# values past ~2^53 cents lose integer precision, which can make the penny
# remainder exceed the participant count (IndexError) or overflow to inf.
MAX_CENTS = 10 ** 11
# Cap weights so amount * weight can't overflow float (→ OverflowError).
# Far above any real weight; with MAX_CENTS this keeps products well finite.
MAX_WEIGHT = 1e12
MAX_ID_LENGTH = 64
# Supported bill size. Backup import rejects anything larger and the UI stops
# adding at these limits, so every bill the app can build can be restored.
# Measured under MicroPython 1.24: settle_up() is quadratic in people (2000
# took 4.4 s) and each render walks every participant entry twice.
MAX_PEOPLE = 200
MAX_ITEMS = 1000
MAX_PARTICIPANT_REFS = 20000
# Names repeat in every item row they appear in ("Among ...", payer), so an
# uncapped name multiplies rendered text by the item count.
MAX_NAME_LENGTH = 60
MAX_DESCRIPTION_LENGTH = 120


def _warn(kind, exc, record, kept):
    # Surface dropped or changed records so silent data loss / dev-time API
    # breaks are visible. PyScript routes stderr to the browser console,
    # where backup import sends users for details, so name the record and
    # say whether it was kept with a change ("fixed") or left out.
    try:
        print("bunnysplit: %s %s record%s: %s" % (
            "fixed" if kept else "skipped malformed", kind,
            " " + record if record else "", exc), file=sys.stderr)
    except Exception:
        pass


def _report_issue(on_issue, kind, exc, record="", kept=False):
    _warn(kind, exc, record, kept)
    if on_issue is not None:
        try:
            on_issue(kind, str(exc))
        except Exception:
            pass


def _valid_weight(value):
    # A finite int or float, the only weights the app saves. A float from an
    # overflowing literal (1e309) is inf, which json.dumps emits as a bare
    # inf/Infinity that json.loads rejects. A string would reach the UI's
    # weight formatting, where MicroPython's int() of a long digit string is
    # quadratic. MicroPython lacks math.isfinite, so test NaN by
    # self-inequality and inf by abs().
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value == value and (
        abs(value) != float("inf"))


def _text(value, kind):
    # str() of a deeply nested list recurses in C; under MicroPython on wasm
    # that overflows the JS stack, which no Python except clause catches.
    if isinstance(value, (list, tuple, dict)):
        raise ValueError(kind + " is not text")
    return str(value)


def _label(d, text_key):
    # Name a raw record in console lines by its id and name or description.
    # Only strings and integers are used (see _text()).
    if not isinstance(d, dict):
        return ""
    parts = []
    rid = d.get("id")
    if isinstance(rid, (str, int)) and not isinstance(rid, bool):
        parts.append("'%s'" % str(rid)[:MAX_ID_LENGTH])
    text = d.get(text_key)
    if isinstance(text, str) and text:
        parts.append("(%s)" % text[:MAX_DESCRIPTION_LENGTH])
    return " ".join(parts)


def _normalize_id(value, kind):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(kind + " id must be a string or integer")
    identifier = str(value)
    if not identifier.strip():
        raise ValueError(kind + " id is empty")
    if len(identifier) > MAX_ID_LENGTH:
        raise ValueError(kind + " id exceeds %d characters" % MAX_ID_LENGTH)
    # MicroPython's jsffi cuts a str at its first NUL when it reaches JS, so
    # the DOM would hold another id ("p2" for "p2\x00", or another person's)
    # and the UI would record shares for the wrong person or for nobody.
    if "\x00" in identifier:
        raise ValueError(kind + " id contains a NUL character")
    return identifier


class Person:
    def __init__(self, id, name):
        self.id = id
        self.name = name

    def to_dict(self):
        return {"id": self.id, "name": self.name}

    @staticmethod
    def from_dict(d):
        return Person(_normalize_id(d["id"], "person"),
                      _text(d["name"], "person name"))


class Item:
    """A single bill line.

    split is either {"mode": MODE_EQUAL} or
    {"mode": MODE_UNEVEN, "weights": {person_id: number}}.
    """

    def __init__(self, id, description, amount_cents, payer_id,
                 participant_ids, split):
        self.id = id
        self.description = description
        self.amount_cents = amount_cents
        self.payer_id = payer_id
        self.participant_ids = list(participant_ids)
        self.split = split

    def split_mode(self):
        if isinstance(self.split, dict):
            return self.split.get("mode", MODE_EQUAL)
        return MODE_EQUAL

    def weights(self):
        if isinstance(self.split, dict):
            w = self.split.get("weights", {})
            if isinstance(w, dict):
                return w
        return {}

    def to_dict(self):
        return {
            "id": self.id,
            "description": self.description,
            "amount_cents": self.amount_cents,
            "payer_id": self.payer_id,
            "participant_ids": list(self.participant_ids),
            "split": self.split,
        }

    @staticmethod
    def from_dict(d, on_issue=None):
        # Dedup participants in source order. Without this, hand-edited
        # or corrupted state with duplicate ids would silently lose
        # money: both split paths key shares by participant_id, so a
        # repeat overwrites the prior share instead of representing a
        # second portion. The UI's checkbox-based picker can't produce
        # duplicates, but the model layer is the trust boundary.
        record = _label(d, "description")
        in_item = "in item " + record if record else ""
        raw_pids = d.get("participant_ids", [])
        if not isinstance(raw_pids, list):
            _report_issue(on_issue, "item", "participant_ids is not a list",
                          record, kept=True)
            raw_pids = []
        seen = set()
        pids = []
        for p in raw_pids:
            try:
                s = _normalize_id(p, "participant")
            except Exception as e:
                _report_issue(on_issue, "participant", e, in_item)
                continue
            if s in seen:
                _report_issue(on_issue, "participant", "duplicate id '%s'" % s,
                              in_item)
                continue
            seen.add(s)
            pids.append(s)

        # Normalize split.mode. Anything not in the known set would
        # raise from split_item() later; render_all() catches that and
        # zeros the results, so the UI looks healthy while the numbers
        # are wrong. Clamp here instead.
        split = d.get("split", {})
        if not isinstance(split, dict):
            _report_issue(on_issue, "item",
                          "split is not an object; using an equal split",
                          record, kept=True)
            split = {"mode": MODE_EQUAL}
        if split.get("mode") not in (MODE_EQUAL, MODE_UNEVEN):
            _report_issue(on_issue, "item",
                          "unknown split mode; using an equal split",
                          record, kept=True)
            split = {"mode": MODE_EQUAL}
        # Keep only what split_item() reads, as values that survive a save
        # and reload: one non-finite number would make the saved JSON
        # unparseable and lose the whole bill. split_item() already treats
        # a non-finite weight as 0, so dropping one leaves the split as is.
        # It reads only participants' weights, and the UI writes no others;
        # extra keys would be parsed again on every load, and building a
        # big dict is superlinear under MicroPython (40,000 keys made each
        # page load take ~12 s), so drop them with a single issue.
        # split_item() caps weights at MAX_WEIGHT; cap them here too (one
        # issue per item), so the UI shows the weight the split uses.
        clean = {"mode": split["mode"]}
        if split["mode"] == MODE_UNEVEN and "weights" in split:
            weights = split["weights"]
            if not isinstance(weights, dict):
                _report_issue(on_issue, "item",
                              "weights is not an object; dropped it",
                              record, kept=True)
                weights = {}
            clean["weights"] = {}
            extra = 0
            capped = 0
            for pid, weight in weights.items():
                if pid not in seen:
                    extra += 1
                elif _valid_weight(weight):
                    if weight > MAX_WEIGHT:
                        capped += 1
                        weight = MAX_WEIGHT
                    clean["weights"][pid] = weight
                else:
                    _report_issue(on_issue, "item",
                                  "dropped invalid weight for '%s'" % pid,
                                  record, kept=True)
            if extra:
                noun = "weight" if extra == 1 else "weights"
                _report_issue(on_issue, "item",
                              "dropped %d %s for non-participants"
                              % (extra, noun), record, kept=True)
            if capped:
                _report_issue(on_issue, "item",
                              "%d %s MAX_WEIGHT; set to MAX_WEIGHT"
                              % (capped, "weight exceeds" if capped == 1
                                 else "weights exceed"), record, kept=True)
        if len(clean) != len(split):
            _report_issue(on_issue, "item", "dropped unknown split fields",
                          record, kept=True)
        split = clean

        # A missing amount is zeroed like a malformed one, and reported: a
        # silent $0.00 would let a backup import look clean.
        if "amount_cents" not in d:
            _report_issue(on_issue, "item",
                          "amount_cents is missing; set to 0",
                          record, kept=True)
            amount = 0
        else:
            amount = d["amount_cents"]
        # bool is an int subclass; exclude it. Reject non-int (e.g. strings
        # from hand-edited storage) so downstream cent math can't crash.
        if isinstance(amount, bool) or not isinstance(amount, int):
            _report_issue(on_issue, "item",
                          "amount_cents is not an integer; set to 0",
                          record, kept=True)
            amount = 0
        # Clamp to [0, MAX_CENTS]. Negatives leak a cent in the uneven
        # path (int() truncates toward zero); oversized values break the
        # float split math. The UI enforces this range; this guards
        # hand-edited / corrupt storage.
        if amount < 0:
            _report_issue(on_issue, "item",
                          "amount_cents is negative; set to 0",
                          record, kept=True)
            amount = 0
        elif amount > MAX_CENTS:
            _report_issue(on_issue, "item",
                          "amount_cents exceeds MAX_CENTS; set to MAX_CENTS",
                          record, kept=True)
            amount = MAX_CENTS

        # Keep the item (and its money) when only the description is bad.
        try:
            description = _text(d.get("description", ""), "description")
        except ValueError as e:
            _report_issue(on_issue, "item", "%s; left blank" % e, record,
                          kept=True)
            description = ""

        return Item(
            _normalize_id(d.get("id", ""), "item"),
            description,
            amount,
            _normalize_id(d.get("payer_id", ""), "payer"),
            pids,
            split,
        )


class AppState:
    def __init__(self, people=None, items=None):
        self.people = people if people is not None else []
        self.items = items if items is not None else []

    def person_by_id(self, pid):
        for p in self.people:
            if p.id == pid:
                return p
        return None

    def items_referencing(self, pid):
        """Items where the person is payer or participant (for safe removal)."""
        out = []
        for it in self.items:
            if it.payer_id == pid or pid in it.participant_ids:
                out.append(it)
        return out

    def to_dict(self):
        return {
            "people": [p.to_dict() for p in self.people],
            "items": [i.to_dict() for i in self.items],
        }

    @staticmethod
    def from_dict(d, on_issue=None):
        if not isinstance(d, dict):
            _report_issue(on_issue, "state", "top-level value is not an object")
            return AppState()
        # Skip individual bad records rather than discarding all saved
        # state. Each skip is logged to stderr so silent data loss is
        # visible in the browser console.
        people = []
        raw_people = d.get("people", [])
        if not isinstance(raw_people, list):
            _report_issue(on_issue, "people", "people is not a list")
            raw_people = []
        person_ids = set()
        for p in raw_people:
            record = _label(p, "name")
            try:
                person = Person.from_dict(p)
            except Exception as e:
                _report_issue(on_issue, "person", e, record)
                continue
            if person.id in person_ids:
                _report_issue(on_issue, "person",
                              "duplicate id '%s'" % person.id, record)
                continue
            person_ids.add(person.id)
            people.append(person)

        valid_pids = person_ids
        items = []
        raw_items = d.get("items", [])
        if not isinstance(raw_items, list):
            _report_issue(on_issue, "items", "items is not a list")
            raw_items = []
        item_ids = set()
        for i in raw_items:
            record = _label(i, "description")
            try:
                it = Item.from_dict(i, on_issue=on_issue)
            except Exception as e:
                _report_issue(on_issue, "item", e, record)
                continue
            # Drop items that reference unknown people. Without this,
            # per_person_totals counts unknown ids but settle_up only
            # walks state.people, so debts/credits to a missing person
            # silently disappear from the transfer plan.
            if it.payer_id not in valid_pids:
                _report_issue(
                    on_issue, "item",
                    "payer '%s' not in roster" % it.payer_id, record)
                continue
            kept = [pid for pid in it.participant_ids if pid in valid_pids]
            if not kept:
                _report_issue(on_issue, "item", "no known participants",
                              record)
                continue
            if len(kept) != len(it.participant_ids):
                _report_issue(on_issue, "item", "dropped unknown participants",
                              record, kept=True)
                it.participant_ids = kept
                if it.split.get("mode") == MODE_UNEVEN:
                    weights = it.split.get("weights", {})
                    if isinstance(weights, dict):
                        it.split = {
                            "mode": MODE_UNEVEN,
                            "weights": {
                                pid: w
                                for pid, w in weights.items()
                                if pid in valid_pids
                            },
                        }
            if it.id in item_ids:
                _report_issue(
                    on_issue, "item", "duplicate id '%s'" % it.id, record)
                continue
            item_ids.add(it.id)
            items.append(it)
        return AppState(people=people, items=items)
