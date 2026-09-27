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


def _warn_skip(kind, exc):
    # Surface dropped records so silent data loss / dev-time API breaks
    # are visible. PyScript routes stderr to the browser console.
    try:
        print("bunnysplit: skipped malformed " + kind + " record: " + str(exc),
              file=sys.stderr)
    except Exception:
        pass


def _report_issue(on_issue, kind, exc):
    _warn_skip(kind, exc)
    if on_issue is not None:
        try:
            on_issue(kind, str(exc))
        except Exception:
            pass


def _json_safe_scalar(value):
    # What json.dumps writes back as standard JSON. A float from an
    # overflowing literal (1e309) is inf, which dumps emits as a bare
    # inf/Infinity that json.loads rejects. MicroPython lacks
    # math.isfinite, so test NaN by self-inequality and inf by abs().
    if value is None or isinstance(value, (bool, int, str)):
        return True
    return isinstance(value, float) and value == value and (
        abs(value) != float("inf"))


def _normalize_id(value, kind):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(kind + " id must be a string or integer")
    identifier = str(value)
    if not identifier.strip():
        raise ValueError(kind + " id is empty")
    if len(identifier) > MAX_ID_LENGTH:
        raise ValueError(kind + " id exceeds %d characters" % MAX_ID_LENGTH)
    return identifier


class Person:
    def __init__(self, id, name):
        self.id = id
        self.name = name

    def to_dict(self):
        return {"id": self.id, "name": self.name}

    @staticmethod
    def from_dict(d):
        return Person(_normalize_id(d["id"], "person"), str(d["name"]))


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
        raw_pids = d.get("participant_ids", [])
        if not isinstance(raw_pids, list):
            _report_issue(on_issue, "item", "participant_ids is not a list")
            raw_pids = []
        seen = set()
        pids = []
        for p in raw_pids:
            try:
                s = _normalize_id(p, "participant")
            except Exception as e:
                _report_issue(on_issue, "participant", e)
                continue
            if s in seen:
                _report_issue(on_issue, "participant", "duplicate id '%s'" % s)
                continue
            seen.add(s)
            pids.append(s)

        # Normalize split.mode. Anything not in the known set would
        # raise from split_item() later; render_all() catches that and
        # zeros the results, so the UI looks healthy while the numbers
        # are wrong. Clamp here instead.
        split = d.get("split", {})
        if not isinstance(split, dict):
            _report_issue(on_issue, "item", "split is not an object")
            split = {"mode": MODE_EQUAL}
        if split.get("mode") not in (MODE_EQUAL, MODE_UNEVEN):
            _report_issue(on_issue, "item", "unknown split mode")
            split = {"mode": MODE_EQUAL}
        # Keep only what split_item() reads, as values that survive a save
        # and reload: one non-finite number would make the saved JSON
        # unparseable and lose the whole bill. split_item() already treats
        # every dropped weight as 0, so the computed split is unchanged.
        clean = {"mode": split["mode"]}
        if split["mode"] == MODE_UNEVEN and "weights" in split:
            weights = split["weights"]
            if not isinstance(weights, dict):
                _report_issue(on_issue, "item", "weights is not an object")
                weights = {}
            clean["weights"] = {}
            for pid, weight in weights.items():
                if _json_safe_scalar(weight):
                    clean["weights"][pid] = weight
                else:
                    _report_issue(on_issue, "item",
                                  "dropped invalid weight for '%s'" % pid)
        if len(clean) != len(split):
            _report_issue(on_issue, "item", "dropped unknown split fields")
        split = clean

        amount = d.get("amount_cents", 0)
        # bool is an int subclass; exclude it. Reject non-int (e.g. strings
        # from hand-edited storage) so downstream cent math can't crash.
        if isinstance(amount, bool) or not isinstance(amount, int):
            _report_issue(on_issue, "item", "amount_cents is not an integer")
            amount = 0
        # Clamp to [0, MAX_CENTS]. Negatives leak a cent in the uneven
        # path (int() truncates toward zero); oversized values break the
        # float split math. The UI enforces this range; this guards
        # hand-edited / corrupt storage.
        if amount < 0:
            _report_issue(on_issue, "item", "amount_cents is negative")
            amount = 0
        elif amount > MAX_CENTS:
            _report_issue(on_issue, "item", "amount_cents exceeds MAX_CENTS")
            amount = MAX_CENTS

        return Item(
            _normalize_id(d.get("id", ""), "item"),
            str(d.get("description", "")),
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
            try:
                person = Person.from_dict(p)
            except Exception as e:
                _report_issue(on_issue, "person", e)
                continue
            if person.id in person_ids:
                _report_issue(
                    on_issue, "person", "duplicate id '%s'" % person.id)
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
            try:
                it = Item.from_dict(i, on_issue=on_issue)
            except Exception as e:
                _report_issue(on_issue, "item", e)
                continue
            # Drop items that reference unknown people. Without this,
            # per_person_totals counts unknown ids but settle_up only
            # walks state.people, so debts/credits to a missing person
            # silently disappear from the transfer plan.
            if it.payer_id not in valid_pids:
                _report_issue(
                    on_issue, "item",
                    "payer '%s' not in roster" % it.payer_id)
                continue
            kept = [pid for pid in it.participant_ids if pid in valid_pids]
            if not kept:
                _report_issue(on_issue, "item", "no known participants")
                continue
            if len(kept) != len(it.participant_ids):
                _report_issue(
                    on_issue,
                    "item",
                    "dropped unknown participants from '%s'" % it.description,
                )
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
                    on_issue, "item", "duplicate id '%s'" % it.id)
                continue
            item_ids.add(it.id)
            items.append(it)
        return AppState(people=people, items=items)
