"""Browser-bound persistence and save-status tests using small PyScript fakes."""

import importlib.util
import json
import pathlib
import re
import sys
import types
import unittest
from unittest import mock

from splitcore.calc import split_item
from splitcore.model import (
    MAX_DESCRIPTION_LENGTH, MAX_ITEMS, MAX_NAME_LENGTH, MAX_PARTICIPANT_REFS,
    MAX_PEOPLE, MAX_WEIGHT, AppState, Item, Person)


ROOT = pathlib.Path(__file__).resolve().parent.parent


class FakeLocalStorage:
    def __init__(self, initial=None):
        self.values = dict(initial or {})

    def getItem(self, key):
        return self.values.get(key)

    def setItem(self, key, value):
        self.values[key] = value

    def removeItem(self, key):
        self.values.pop(key, None)


class FakeConsole:
    def __init__(self):
        self.warnings = []
        self.calls = []  # raw arguments, as JS receives them

    def warn(self, *parts):
        self.warnings.append(" ".join(str(part) for part in parts))
        self.calls.append(parts)

    def log(self, message):
        pass


def load_storage(local_storage):
    pyscript = types.ModuleType("pyscript")
    pyscript.window = types.SimpleNamespace(
        localStorage=local_storage,
        console=FakeConsole(),
    )
    with mock.patch.dict(sys.modules, {"pyscript": pyscript}):
        spec = importlib.util.spec_from_file_location(
            "storage_under_test", ROOT / "storage.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class StorageRecoveryTests(unittest.TestCase):
    def test_invalid_json_is_backed_up_and_reported(self):
        local = FakeLocalStorage({"bunnysplit": "{broken"})
        storage = load_storage(local)

        state = storage.load()

        self.assertEqual(state.to_dict(), {"people": [], "items": []})
        self.assertEqual(local.values["bunnysplit:corrupt"], "{broken")
        self.assertIn("Recovered corrupt saved data", storage.recovery_warning())

    def test_partial_model_recovery_is_backed_up_and_reported(self):
        raw = json.dumps({
            "people": [
                {"id": "a", "name": "A"},
                {"id": "a", "name": "duplicate"},
            ],
            "items": "not-a-list",
        })
        local = FakeLocalStorage({"bunnysplit": raw})
        storage = load_storage(local)

        state = storage.load()

        self.assertEqual([person.name for person in state.people], ["A"])
        self.assertEqual(state.items, [])
        self.assertEqual(local.values["bunnysplit:corrupt"], raw)
        self.assertIn("Recovered malformed saved data",
                      storage.recovery_warning())

    def test_existing_backup_remains_visible_and_is_not_overwritten(self):
        valid = json.dumps({"people": [], "items": []})
        local = FakeLocalStorage({
            "bunnysplit": valid,
            "bunnysplit:corrupt": "original backup",
        })
        storage = load_storage(local)

        storage.load()
        storage.save(AppState())

        self.assertIn("backup is preserved", storage.recovery_warning())
        self.assertEqual(local.values["bunnysplit:corrupt"], "original backup")

    def test_later_corrupt_payload_gets_a_versioned_backup(self):
        current_corrupt = "{current-corrupt"
        local = FakeLocalStorage({
            "bunnysplit": current_corrupt,
            "bunnysplit:corrupt": "older backup",
        })
        storage = load_storage(local)

        storage.load()
        storage.load()  # Reloading the same payload must not duplicate it.
        storage.save(AppState())

        self.assertEqual(local.values["bunnysplit:corrupt"], "older backup")
        self.assertEqual(local.values["bunnysplit:corrupt:1"], current_corrupt)
        self.assertNotIn("bunnysplit:corrupt:2", local.values)
        self.assertIn("bunnysplit:corrupt:1", storage.recovery_warning())

    def test_saved_weight_above_the_cap_loads_without_a_recovery_warning(self):
        # The previous release saved typed weights above the cap (and
        # split_item() capped them), so capping one on load changes no share
        # and must not flag the bill as corrupt; the warning and the corrupt
        # key would never clear.
        raw = json.dumps({
            "people": [{"id": "p1", "name": "Ann"},
                       {"id": "p2", "name": "Bob"}],
            "items": [{
                "id": "i3", "description": "Rent", "amount_cents": 100000,
                "payer_id": "p1", "participant_ids": ["p1", "p2"],
                "split": {"mode": "uneven",
                          "weights": {"p1": 2000000000000.0, "p2": 1.0}},
            }],
        })
        local = FakeLocalStorage({"bunnysplit": raw})
        storage = load_storage(local)

        state = storage.load()

        self.assertEqual(state.items[0].weights(),
                         {"p1": MAX_WEIGHT, "p2": 1.0})
        self.assertEqual(split_item(state.items[0]),
                         {"p1": 100000, "p2": 0})
        self.assertEqual(storage.recovery_warning(), "")
        self.assertEqual(list(local.values), ["bunnysplit"])
        # Importing the same bill still says the file was changed.
        _, issues = storage.parse_backup(raw)
        self.assertEqual(len(issues), 1)
        self.assertIn("MAX_WEIGHT", issues[0])


class BackupFormatTests(unittest.TestCase):
    def setUp(self):
        self.local = FakeLocalStorage()
        self.storage = load_storage(self.local)

    def test_backup_format_round_trips_and_matches_saved_state(self):
        state = AppState(
            people=[Person("p1", "A"), Person("p2", "B")],
            items=[Item("i1", "x", 1234, "p1", ["p1", "p2"],
                        {"mode": "equal"})],
        )

        text = self.storage.dumps(state)
        restored, issues = self.storage.parse_backup(text)
        self.storage.save(state)

        self.assertEqual(restored.to_dict(), state.to_dict())
        self.assertEqual(issues, [])
        self.assertEqual(self.local.values["bunnysplit"], text)

    def test_text_that_is_not_json_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self.storage.parse_backup("{not json")

        self.assertEqual(str(ctx.exception), "That file isn't valid JSON.")

    def test_json_without_people_and_items_lists_is_rejected(self):
        # AppState.from_dict() would read each of these as an empty bill.
        for text in ("[1, 2, 3]", "{}",
                     json.dumps({"name": "pkg", "version": "1.0"}),
                     json.dumps({"people": None, "items": []}),
                     json.dumps({"people": [], "items": {}})):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as ctx:
                    self.storage.parse_backup(text)
                self.assertIn("isn't a bunnysplit backup", str(ctx.exception))

    def test_oversized_text_is_rejected_before_parsing(self):
        text = " " * (self.storage.MAX_BACKUP_BYTES + 1)

        with mock.patch.object(self.storage.json, "loads") as loads:
            with self.assertRaises(ValueError) as ctx:
                self.storage.parse_backup(text)

        loads.assert_not_called()
        self.assertIn("too large", str(ctx.exception))

    def test_long_unquoted_number_is_rejected_before_parsing(self):
        # MicroPython converts a huge integer literal in quadratic time, so
        # the digit run must be refused before json.loads() sees it. (CPython
        # would reject >4300 digits itself, which hides the bug; 100 doesn't.)
        text = '{"people": [], "items": [], "x": ' + "7" * 100 + "}"

        with mock.patch.object(self.storage.json, "loads") as loads:
            with self.assertRaises(ValueError) as ctx:
                self.storage.parse_backup(text)

        loads.assert_not_called()
        self.assertIn("number too long", str(ctx.exception))

    def test_digit_limit_is_the_longest_legal_integer_id(self):
        limit = self.storage.MAX_NUMBER_DIGITS
        ok = '{"people": [], "items": [], "x": ' + "7" * limit + "}"
        too_long = '{"people": [], "items": [], "x": ' + "7" * (limit + 1) + "}"

        self.storage.parse_backup(ok)
        with self.assertRaises(ValueError):
            self.storage.parse_backup(too_long)

    def test_long_digit_runs_inside_strings_are_allowed(self):
        description = 'say "hi" ' + "1" * 100
        text = json.dumps({
            "people": [{"id": "p1", "name": "A"}],
            "items": [{"id": "i1", "description": description,
                       "amount_cents": 100, "payer_id": "p1",
                       "participant_ids": ["p1"], "split": {"mode": "equal"}}],
        })

        state, issues = self.storage.parse_backup(text)

        self.assertEqual(state.items[0].description, description)
        self.assertEqual(issues, [])

    def test_escaped_backslash_before_a_quote_still_closes_the_string(self):
        # "end\\" is the string end\ followed by a real closing quote, so
        # the digits after it are an unquoted number.
        text = (json.dumps({"people": [], "items": [], "n": "end\\"})[:-1]
                + ', "x": ' + "1" * 100 + "}")

        with self.assertRaises(ValueError) as ctx:
            self.storage.parse_backup(text)

        self.assertIn("number too long", str(ctx.exception))

    def test_malformed_unicode_escape_is_rejected_before_parsing(self):
        # MicroPython's json.loads reads the four characters after \u as hex
        # whatever they are, even a '"', so to it "\u"abc" is one string and
        # the digits below are an unquoted number the digit scan would miss.
        text = ('{"people": [], "items": [], "x": "\\u"abc", "y": '
                + "7" * 100 + "}")

        with mock.patch.object(self.storage.json, "loads") as loads:
            with self.assertRaises(ValueError) as ctx:
                self.storage.parse_backup(text)

        loads.assert_not_called()
        self.assertEqual(str(ctx.exception), "That file isn't valid JSON.")

    def test_standard_escapes_still_import(self):
        # A control character, a quote, a literal backslash-u and an accent,
        # written both escaped and raw.
        name = 'A\u0001"\\u\u00e9'
        for ensure_ascii in (True, False):
            with self.subTest(ensure_ascii=ensure_ascii):
                text = json.dumps(
                    {"people": [{"id": "p1", "name": name}], "items": []},
                    ensure_ascii=ensure_ascii)

                state, issues = self.storage.parse_backup(text)

                self.assertEqual(state.people[0].name, name)
                self.assertEqual(issues, [])

    def test_surrogate_pair_escapes_are_decoded_before_json_loads(self):
        # MicroPython's json.loads decodes each half of \ud83d\ude00 on its
        # own into an invalid lone surrogate, which the browser shows and
        # saves as U+FFFD garbage. CPython's json.dumps writes emoji that
        # way by default, so pairs must be combined before parsing.
        text = json.dumps({
            "people": [{"id": "p1", "name": "Ann \U0001F600"}],
            "items": [{"id": "i1", "description": "Party \U0001F389",
                       "amount_cents": 100, "payer_id": "p1",
                       "participant_ids": ["p1"], "split": {"mode": "equal"}}],
        }).replace("\\ud83c\\udf89", "\\uD83C\\uDF89")
        self.assertIn("\\ud83d\\ude00", text)
        parsed = []
        real_loads = json.loads

        def loads(s):
            parsed.append(s)
            return real_loads(s)

        with mock.patch.object(self.storage.json, "loads", loads):
            state, issues = self.storage.parse_backup(text)

        self.assertNotIn("\\ud", parsed[0].lower())
        self.assertEqual(state.people[0].name, "Ann \U0001F600")
        self.assertEqual(state.items[0].description, "Party \U0001F389")
        self.assertEqual(issues, [])

    def test_unpaired_surrogate_escapes_become_replacement_characters(self):
        text = ('{"people": [{"id": "p1", "name": "a\\ud83d b\\ude00'
                ' c\\ude00\\ud83d"}], "items": []}')

        state, issues = self.storage.parse_backup(text)

        self.assertEqual(state.people[0].name, "a\ufffd b\ufffd c\ufffd\ufffd")
        self.assertEqual(len(issues), 1)

    def test_escaped_backslash_before_ud_is_left_alone(self):
        # "\\ud83d" in JSON is a literal backslash followed by "ud83d".
        name = "\\ud83d\\ude00 \\\U0001F600"
        text = json.dumps({"people": [{"id": "p1", "name": name}],
                           "items": []})

        state, issues = self.storage.parse_backup(text)

        self.assertEqual(state.people[0].name, name)
        self.assertEqual(issues, [])

    def test_size_is_measured_in_utf8_bytes_of_the_decoded_text(self):
        # Blob.text() decodes each invalid byte of a file to U+FFFD: one
        # character but three UTF-8 bytes, so a file within the byte limit
        # could otherwise hand json.loads three times as much text.
        limit = self.storage.MAX_BACKUP_BYTES
        text = ('{"people": [], "items": [], "x": "'
                + "\ufffd" * (limit - 40) + '"}')
        self.assertLessEqual(len(text), limit)

        with mock.patch.object(self.storage.json, "loads") as loads:
            with self.assertRaises(ValueError) as ctx:
                self.storage.parse_backup(text)

        loads.assert_not_called()
        self.assertIn("too large", str(ctx.exception))

    def test_multibyte_text_exactly_at_the_byte_limit_is_accepted(self):
        head, tail = '{"people": [], "items": [], "x": "', '"}'
        room = self.storage.MAX_BACKUP_BYTES - len(head) - len(tail)
        text = head + "\u00e9" * (room // 2) + "x" * (room % 2) + tail
        self.assertEqual(len(text.encode()), self.storage.MAX_BACKUP_BYTES)

        state, issues = self.storage.parse_backup(text)

        self.assertEqual(issues, [])

    def test_record_counts_are_limited_before_building_state(self):
        people = [{"id": "p%d" % i, "name": "P%d" % i}
                  for i in range(MAX_PEOPLE + 1)]
        items = [{"id": "i%d" % i} for i in range(MAX_ITEMS + 1)]
        cases = (
            ({"people": people, "items": []}, "at most %d people" % MAX_PEOPLE),
            ({"people": [], "items": items}, "at most %d items" % MAX_ITEMS),
        )
        for raw, message in cases:
            with self.subTest(message=message):
                with mock.patch.object(self.storage.AppState,
                                       "from_dict") as from_dict:
                    with self.assertRaises(ValueError) as ctx:
                        self.storage.parse_backup(json.dumps(raw))
                from_dict.assert_not_called()
                self.assertIn(message, str(ctx.exception))

    def test_record_counts_at_the_limits_are_accepted(self):
        people = [{"id": "p%d" % i, "name": "P%d" % i}
                  for i in range(MAX_PEOPLE)]
        per_item = MAX_PARTICIPANT_REFS // MAX_ITEMS
        pids = ["p%d" % i for i in range(per_item)]
        items = [{"id": "i%d" % i, "description": "x", "amount_cents": 100,
                  "payer_id": "p0", "participant_ids": pids,
                  "split": {"mode": "equal"}} for i in range(MAX_ITEMS)]

        state, issues = self.storage.parse_backup(
            json.dumps({"people": people, "items": items}))

        self.assertEqual(len(state.people), MAX_PEOPLE)
        self.assertEqual(len(state.items), MAX_ITEMS)
        self.assertEqual(issues, [])

    def test_participant_entries_are_limited_across_items(self):
        # Each item within its own limit (one entry per person).
        pids = ["p%d" % i for i in range(MAX_PEOPLE)]
        items = [{"id": "i%d" % i, "participant_ids": pids}
                 for i in range(MAX_PARTICIPANT_REFS // MAX_PEOPLE + 1)]

        with mock.patch.object(self.storage.AppState, "from_dict") as from_dict:
            with self.assertRaises(ValueError) as ctx:
                self.storage.parse_backup(
                    json.dumps({"people": [], "items": items}))

        from_dict.assert_not_called()
        self.assertIn("at most %d participant entries" % MAX_PARTICIPANT_REFS,
                      str(ctx.exception))

    def test_names_and_descriptions_are_capped(self):
        # len() counts code points; the inputs' HTML maxlength counts UTF-16
        # units, so the browser is never looser than this check.
        def backup(name, description):
            return json.dumps({
                "people": [{"id": "p1", "name": name}],
                "items": [{"id": "i1", "description": description,
                           "amount_cents": 100, "payer_id": "p1",
                           "participant_ids": ["p1"],
                           "split": {"mode": "equal"}}],
            })

        self.storage.parse_backup(
            backup("n" * MAX_NAME_LENGTH, "d" * MAX_DESCRIPTION_LENGTH))
        cases = (
            (backup("n" * (MAX_NAME_LENGTH + 1), "d"),
             "Names can be at most %d characters." % MAX_NAME_LENGTH),
            (backup("n", "d" * (MAX_DESCRIPTION_LENGTH + 1)),
             "Descriptions can be at most %d characters."
             % MAX_DESCRIPTION_LENGTH),
        )
        for text, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as ctx:
                    self.storage.parse_backup(text)
                self.assertEqual(str(ctx.exception), message)

    def test_backup_whose_reexport_exceeds_the_limit_is_rejected(self):
        # Integer ids come back quoted, so this state's export is larger than
        # the file itself: the file passes the pre-read size check and must
        # still be refused, or its own export could not be restored.
        text = json.dumps({"people": [{"id": 1, "name": "A"}], "items": []})
        exported = len(self.storage.dumps(AppState(people=[Person("1", "A")])))
        self.assertGreater(exported, len(text))

        with mock.patch.object(self.storage, "MAX_BACKUP_BYTES", len(text)):
            with self.assertRaises(ValueError) as ctx:
                self.storage.parse_backup(text)

        self.assertIn("once restored", str(ctx.exception))
        self.assertNotIn("back up", str(ctx.exception))

    def test_a_compact_file_is_refused_with_its_size_once_restored(self):
        # The app writes ", " and ": " separators, so a minified backup grows
        # when restored: this one meets the limit but its restored bill does
        # not. The message must not cite a limit the file already meets.
        raw = {"people": [{"id": "p%d" % i, "name": "P%d" % i}
                          for i in range(78)], "items": []}
        text = json.dumps(raw, separators=(",", ":"))

        with mock.patch.object(self.storage, "MAX_BACKUP_BYTES", 2048):
            self.assertLessEqual(len(text.encode()), 2048)
            with self.assertRaises(ValueError) as ctx:
                self.storage.parse_backup(text)

        self.assertEqual(
            str(ctx.exception),
            "That backup would be 3 KiB once restored, over the 2 KiB limit.")

    def test_a_bill_at_every_limit_round_trips(self):
        pids = ["p%d" % i for i in range(MAX_PEOPLE)]
        people = [Person(pid, "n" * MAX_NAME_LENGTH) for pid in pids]
        items = [Item("i0", "d" * MAX_DESCRIPTION_LENGTH, 100, "p0", pids,
                      {"mode": "uneven",
                       "weights": {pid: 1.5 for pid in pids}})]
        # The other items share the remaining participant entries exactly.
        rest = MAX_PARTICIPANT_REFS - MAX_PEOPLE
        per_item, extra = divmod(rest, MAX_ITEMS - 1)
        for i in range(1, MAX_ITEMS):
            count = per_item + (1 if i <= extra else 0)
            items.append(Item("i%d" % i, "x", 100, "p0", pids[:count],
                              {"mode": "equal"}))
        state = AppState(people=people, items=items)
        self.assertEqual(sum(len(i.participant_ids) for i in items),
                         MAX_PARTICIPANT_REFS)

        restored, issues = self.storage.parse_backup(
            self.storage.check_restorable(state))

        self.assertEqual(restored.to_dict(), state.to_dict())
        self.assertEqual(issues, [])

    def test_an_item_with_more_participants_than_people_is_rejected(self):
        # MicroPython's str hash is 16 bits and its sets probe linearly, so
        # Item.from_dict()'s dedup set is quadratic in one item's ids when
        # they share a hash: 20,000 of them, within the total limit, froze
        # an import for ~10 s. No item can have more participants than the
        # roster has people.
        pids = ["p%d" % i for i in range(MAX_PEOPLE + 1)]
        raw = {"people": [], "items": [{"id": "i1", "participant_ids": pids}]}

        with mock.patch.object(self.storage.AppState, "from_dict") as from_dict:
            with self.assertRaises(ValueError) as ctx:
                self.storage.parse_backup(json.dumps(raw))

        from_dict.assert_not_called()
        self.assertEqual(str(ctx.exception),
                         "A backup item can have at most %d participants."
                         % MAX_PEOPLE)

    def test_an_item_with_more_weights_than_people_is_rejected(self):
        weights = {"p%d" % i: 1 for i in range(MAX_PEOPLE + 1)}
        raw = {"people": [], "items": [{
            "id": "i1", "participant_ids": ["p0"],
            "split": {"mode": "uneven", "weights": weights}}]}

        with mock.patch.object(self.storage.AppState, "from_dict") as from_dict:
            with self.assertRaises(ValueError) as ctx:
                self.storage.parse_backup(json.dumps(raw))

        from_dict.assert_not_called()
        self.assertEqual(str(ctx.exception),
                         "A backup item can have at most %d split weights."
                         % MAX_PEOPLE)

    def test_objects_with_too_many_members_are_rejected_before_parsing(self):
        # json.loads builds an object whose keys share a MicroPython hash in
        # quadratic time (47,000 keys in an ignored field took ~30 s), before
        # any check on the parsed value can run. MicroPython's json.loads
        # also reads ',' and ':' as whitespace, lets ']' close '{' and takes
        # any value as a key, so members are counted as values, not colons.
        limit = self.storage.MAX_OBJECT_MEMBERS
        head = '{"people": [], "items": [], "x": {'
        members = ['"k%d": 0' % i for i in range(limit + 1)]
        cases = {
            "top level": '{"people": [], "items": [], %s}' % ", ".join(
                members[:limit - 1]),
            "nested": head + ", ".join(members) + "}}",
            "object values": head + ", ".join(
                '"k%d": {}' % i for i in range(limit + 1)) + "}}",
            "after a nested value": head + '"a": [[[1]]], ' + ", ".join(
                members) + "}}",
            "no colons or commas": '{"people" [] "items" [] "x" {' + " ".join(
                '"k%d" 0' % i for i in range(limit + 1)) + "}}",
            "number keys, no separators": '{"people" [] "items" [] "x" {'
                + "".join("%dtrue" % i for i in range(limit + 1)) + "}}",
            "closed by a bracket": head + ", ".join(members) + "]}",
            "never closed": head + ", ".join(members),
        }
        for label, text in cases.items():
            with self.subTest(label):
                with mock.patch.object(self.storage.json, "loads") as loads:
                    with self.assertRaises(ValueError) as ctx:
                        self.storage.parse_backup(text)
                loads.assert_not_called()
                self.assertEqual(
                    str(ctx.exception), "That file has an object with too "
                    "many fields to be a bunnysplit backup.")

    def test_objects_at_the_member_limit_are_parsed(self):
        limit = self.storage.MAX_OBJECT_MEMBERS
        self.assertGreaterEqual(limit, MAX_PEOPLE)  # one weight per person
        extra = ", ".join('"k%d": {}' % i for i in range(limit - 2))
        nested = ", ".join('"k%d": 0' % i for i in range(limit))

        for text in ('{"people": [], "items": [], %s}' % extra,
                     '{"people": [], "items": [], "x": {%s}}' % nested):
            state, issues = self.storage.parse_backup(text)
            self.assertEqual(issues, [])

    def test_too_many_objects_and_lists_are_rejected_before_parsing(self):
        # Counting members walks every '{' and '[' in Python, which is slow
        # under MicroPython; json.loads itself reads 512 KiB of them in ~40 ms.
        limit = self.storage.MAX_CONTAINERS

        def backup(lists):
            # The top object, "people", "items" and "x": four containers.
            return ('{"people": [], "items": [], "x": [%s]}'
                    % ", ".join(["[]"] * (lists - 4)))

        self.storage.parse_backup(backup(limit))
        with mock.patch.object(self.storage.json, "loads") as loads:
            with self.assertRaises(ValueError) as ctx:
                self.storage.parse_backup(backup(limit + 1))

        loads.assert_not_called()
        self.assertEqual(str(ctx.exception), "That file has too many objects "
                         "and lists to be a bunnysplit backup.")

    def test_text_json_would_reject_is_refused_before_parsing(self):
        # The member count relies on every character outside strings being
        # one json.loads accepts; anything else makes json.loads fail anyway.
        for text in ('{"people": [], "items": [], "x": NaN}',
                     '{"people": [], "items": []} x',
                     '{"people": [], "items": [], "x": nul}'):
            with self.subTest(text=text):
                with mock.patch.object(self.storage.json, "loads") as loads:
                    with self.assertRaises(ValueError) as ctx:
                        self.storage.parse_backup(text)
                loads.assert_not_called()
                self.assertEqual(str(ctx.exception),
                                 "That file isn't valid JSON.")

    def test_check_restorable_returns_the_export_text(self):
        state = AppState(people=[Person("p1", "A")])

        self.assertEqual(self.storage.check_restorable(state),
                         self.storage.dumps(state))

    def test_export_size_is_measured_in_utf8_bytes(self):
        text = "\U0001F600" * 10  # 10 code points, 40 bytes

        with mock.patch.object(self.storage, "MAX_BACKUP_BYTES", 39):
            with self.assertRaises(ValueError):
                self.storage.check_export_size(text)
        with mock.patch.object(self.storage, "MAX_BACKUP_BYTES", 40):
            self.storage.check_export_size(text)

    def test_save_can_reuse_already_serialized_text(self):
        self.storage.save(AppState(), text="precomputed")

        self.assertEqual(self.local.values["bunnysplit"], "precomputed")

    def test_negative_size_counts_as_too_large(self):
        # MicroPython's jsffi truncates JS numbers to int32, so a multi-GiB
        # File.size can arrive negative.
        with self.assertRaises(ValueError) as ctx:
            self.storage.check_backup_size(-2147483648)

        self.assertIn("too large", str(ctx.exception))

    def test_recovered_records_are_reported_as_issues(self):
        text = json.dumps({
            "people": [
                {"id": "p1", "name": "A"},
                {"id": "p1", "name": "duplicate"},
            ],
            "items": [],
        })

        state, issues = self.storage.parse_backup(text)

        self.assertEqual([person.name for person in state.people], ["A"])
        self.assertEqual(len(issues), 1)

    def test_weights_above_the_cap_are_capped_and_reported(self):
        # Every limit admits 20,000 weights; at 1e308 each one took ~0.5 ms
        # to show under MicroPython, on every render and page load.
        text = ('{"people":[{"id":"p1","name":"Ann"},{"id":"p2","name":"Bob"}],'
                '"items":[{"id":"i1","description":"x","amount_cents":500,'
                '"payer_id":"p1","participant_ids":["p1","p2"],"split":'
                '{"mode":"uneven","weights":{"p1":1e308,"p2":1e12}}}]}')

        state, issues = self.storage.parse_backup(text)

        self.assertEqual(state.items[0].weights(),
                         {"p1": MAX_WEIGHT, "p2": MAX_WEIGHT})
        self.assertEqual(
            issues, ["item: 1 weight is outside +/-MAX_WEIGHT; "
                     "set to the nearest limit"])

    def test_weight_below_minus_the_cap_is_capped_and_restores_cleanly(self):
        # MicroPython's json.dumps writes -1.7976931348623157e308 with 16
        # digits, past the largest float, so kept verbatim it passed export
        # and reloaded as -inf: re-import reported a problem and every page
        # load warned of corrupt data.
        text = ('{"people":[{"id":"p1","name":"A"},{"id":"p2","name":"B"}],'
                '"items":[{"id":"i1","description":"x","amount_cents":1000,'
                '"payer_id":"p1","participant_ids":["p1","p2"],"split":'
                '{"mode":"uneven","weights":'
                '{"p1":-1.7976931348623157e308,"p2":1}}}]}')

        state, issues = self.storage.parse_backup(text)

        self.assertEqual(state.items[0].weights(),
                         {"p1": -MAX_WEIGHT, "p2": 1})
        self.assertEqual(len(issues), 1)
        self.assertIn("MAX_WEIGHT", issues[0])
        restored, issues = self.storage.parse_backup(
            self.storage.check_restorable(state))
        self.assertEqual(restored.items[0].weights(),
                         {"p1": -MAX_WEIGHT, "p2": 1})
        self.assertEqual(issues, [])

    def test_escaped_nul_in_an_id_is_skipped_and_reported(self):
        # \u0000 has four hex digits, so it passes the escape check; the id
        # it decodes into must not reach the DOM (see the model test).
        text = ('{"people":[{"id":"p1","name":"Ann"},'
                '{"id":"p2\\u0000","name":"Bob"}],"items":[]}')

        state, issues = self.storage.parse_backup(text)

        self.assertEqual([person.id for person in state.people], ["p1"])
        self.assertEqual(len(issues), 1)
        self.assertIn("NUL", issues[0])


class FakeClassList:
    def __init__(self):
        self.values = set()

    def add(self, value):
        self.values.add(value)

    def remove(self, value):
        self.values.discard(value)


class FakePill:
    def __init__(self):
        self.label = types.SimpleNamespace(textContent="")
        self.classList = FakeClassList()
        self.title = ""

    def querySelector(self, selector):
        return self.label if selector == ".saved-label" else None


class FakeElement:
    def __init__(self, tag=""):
        self.tag = tag
        self.children = []
        self.appended = []
        self.textContent = ""
        self.className = ""
        self.href = None
        self.download = None
        self.value = ""
        self.click_count = 0

    def appendChild(self, child):
        self.children.append(child)
        self.appended.append(child)

    def removeChild(self, child):
        self.children.remove(child)

    def click(self):
        self.click_count += 1


class FakeDocument:
    def __init__(self, extra_elements=None, extra_lists=None):
        self.body = FakeElement("body")
        self._extra = extra_elements or {}
        self._lists = extra_lists or {}

    def querySelector(self, selector):
        return self._extra.get(selector)

    def querySelectorAll(self, selector):
        return FakeFileList(self._lists.get(selector, []))

    def createElement(self, tag):
        return FakeElement(tag)


class FakeJsError(BaseException):
    """A JS exception reaching Python through MicroPython's jsffi: it unwinds
    through Python frames without running `except Exception`."""


class FakeJsProxy:
    """A JS object (e.g. a DOMException) reaching Python through jsffi: str()
    of it hides its name and message."""

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return "<JsProxy 7>"


class FakePromise:
    def __init__(self, value=None, error=None):
        self._value = value
        self._error = error
        self.callbacks = None
        self.catch_callback = None
        self.rejection = None

    def then(self, on_ok, on_err):
        # Settles synchronously; a browser settles after the change handler
        # returns, so import callbacks must not depend on either ordering.
        # Anything a handler throws rejects the chained promise; this fake
        # returns itself as that promise.
        self.callbacks = (on_ok, on_err)
        try:
            if self._error is not None:
                on_err(self._error)
            else:
                on_ok(self._value)
        except BaseException as e:
            self.rejection = e
        return self

    def catch(self, on_err):
        self.catch_callback = on_err
        if self.rejection is not None:
            on_err(self.rejection)
        return self


class FakeFile:
    def __init__(self, text_value=None, error=None, size=None):
        self._text_value = text_value
        self._error = error
        self.size = len(text_value or "") if size is None else size
        self.slices = []
        self.promises = []

    def slice(self, start, end):
        self.slices.append((start, end))
        text = None if self._text_value is None else self._text_value[start:end]
        blob = FakeFile(text, self._error)
        blob.promises = self.promises  # reads through a slice stay visible
        return blob

    def text(self):
        promise = FakePromise(self._text_value, self._error)
        self.promises.append(promise)
        return promise


class FakeFileList:
    def __init__(self, files):
        self._files = files
        self.length = len(files)

    def item(self, index):
        return self._files[index]


class FakeFileInput:
    def __init__(self, files):
        self.files = FakeFileList(files)
        self.value = "sentinel.json"


def load_ui(pill, extra_elements=None, extra_lists=None):
    elements = dict(extra_elements or {})
    elements[".saved-pill"] = pill
    pyscript = types.ModuleType("pyscript")
    pyscript.document = FakeDocument(elements, extra_lists)
    pyscript.window = types.SimpleNamespace(
        console=FakeConsole(), encodeURIComponent=lambda s: s)
    ffi = types.ModuleType("pyscript.ffi")
    ffi.create_proxy = lambda function: function
    with mock.patch.dict(
            sys.modules, {"pyscript": pyscript, "pyscript.ffi": ffi}):
        spec = importlib.util.spec_from_file_location(
            "ui_under_test", ROOT / "ui.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class UiBoundaryTests(unittest.TestCase):
    def test_recovery_warning_is_visible_in_saved_pill(self):
        pill = FakePill()
        ui = load_ui(pill)
        ui._storage = types.SimpleNamespace(
            recovery_warning=lambda: "Recovered data; backup preserved.")

        ui._set_save_status(True)

        self.assertEqual(pill.label.textContent, "Recovered corrupt data")
        self.assertIn("err", pill.classList.values)
        self.assertEqual(pill.title, "Recovered data; backup preserved.")

    def test_id_generation_checks_collisions_without_parsing_loaded_ids(self):
        pill = FakePill()
        ui = load_ui(pill)
        ui._state = AppState(
            people=[Person("p1", "A"), Person("custom" + "9" * 5000, "B")],
            items=[Item("p2", "x", 1, "p1", ["p1"], {"mode": "equal"})],
        )

        ui._seed_counter()

        self.assertEqual(ui._next_id("p"), "p3")

    def test_weights_are_shown_as_the_split_uses_them(self):
        # split_item() reads weights through parse_finite(); a long digit
        # string must never reach int(), which MicroPython runs in
        # quadratic time on every render.
        ui = load_ui(FakePill())
        cases = ((2.0, "2"), (3, "3"), (1.5, "1.5"), (float("inf"), "0"),
                 (float("nan"), "0"), (-1, "0"), (None, "0"),
                 ("7" * 400, "0"))
        for weight, shown in cases:
            with self.subTest(weight=weight):
                self.assertEqual(ui._fmt_weight(weight), shown)

    def test_weights_above_the_cap_are_shown_as_the_split_uses_them(self):
        # split_item() caps weights at MAX_WEIGHT. Formatting the raw value
        # showed a 309-digit badge next to an even split and built a bigint
        # per weight, ~0.5 ms each under MicroPython on every render.
        ui = load_ui(FakePill())
        cap = ui._fmt_weight(MAX_WEIGHT)
        self.assertEqual(cap, "1000000000000")
        for weight in (MAX_WEIGHT + 1, 5e12, 10 ** 40, 1e308,
                       1.7976931348623157e308):
            with self.subTest(weight=weight):
                self.assertEqual(ui._fmt_weight(weight), cap)
        both = Item("i", "x", 1000, "a", ["a", "b"],
                    {"mode": "uneven", "weights": {"a": 1e308, "b": 1e12}})
        self.assertEqual(ui._weights_summary(both), cap + "·" + cap)


class DomElement(FakeElement):
    """Enough of an element for start() and render_all()."""

    def __init__(self, tag=""):
        super().__init__(tag)
        self.style = types.SimpleNamespace(setProperty=lambda name, value: None)
        self.classList = FakeClassList()
        self.listeners = []
        self.removed = False

    @property
    def firstChild(self):
        return self.children[0] if self.children else None

    def setAttribute(self, name, value):
        pass

    def addEventListener(self, event, handler):
        self.listeners.append(event)

    def remove(self):
        self.removed = True


class DomDocument(FakeDocument):
    def createElement(self, tag):
        return DomElement(tag)


class OutdatedShellTests(unittest.TestCase):
    def test_start_renders_the_saved_bill_without_the_backup_panel(self):
        # A service-worker update can run this ui.py under the cached
        # index.html from before the Backup panel existed. The bill must
        # still render; the panel shows up on the next load.
        ids = set(re.findall(r'\bid="([^"]+)"',
                             (ROOT / "index.html").read_text()))
        backup_ids = {"export-state", "import-state", "import-file",
                      "backup-status"}
        self.assertLessEqual(backup_ids, ids)
        elements = {"#" + i: DomElement() for i in ids - backup_ids}
        elements[".saved-pill"] = FakePill()
        ui = load_ui(FakePill())
        ui.document = DomDocument(elements)
        state = AppState(
            people=[Person("p1", "Ann"), Person("p2", "Bob")],
            items=[Item("i1", "Dinner", 3000, "p1", ["p1", "p2"],
                        {"mode": "equal"})])

        ui.start(state, load_storage(FakeLocalStorage()))

        self.assertTrue(elements["#boot-msg"].removed)
        self.assertEqual(len(elements["#people-list"].children), 2)
        self.assertEqual(len(elements["#items-list"].children), 1)
        self.assertEqual(elements["#kpi-total"].textContent, "$30.00")
        self.assertEqual(ui.window.console.warnings, [])


BACKUP = json.dumps({
    "people": [{"id": "p1", "name": "A"}],
    "items": [{
        "id": "i1", "description": "x", "amount_cents": 500,
        "payer_id": "p1", "participant_ids": ["p1"],
        "split": {"mode": "equal"},
    }],
})


class ImportExportTests(unittest.TestCase):
    def setUp(self):
        self.status = FakeElement()
        self.ui = load_ui(FakePill(), {"#backup-status": self.status})
        self.local = FakeLocalStorage()
        self.ui._storage = load_storage(self.local)
        self.ui._state = AppState()
        self.prompts = []
        self.confirm_answer = True

        def confirm(message):
            self.prompts.append(message)
            return self.confirm_answer

        self.ui.window.confirm = confirm

    def import_file(self, fake_file):
        field = FakeFileInput([fake_file])
        self.ui.on_import_file_change(types.SimpleNamespace(target=field))
        return field

    def import_text(self, text):
        return self.import_file(FakeFile(text_value=text))

    def saved(self):
        raw = self.local.values.get("bunnysplit")
        return None if raw is None else json.loads(raw)

    def test_export_downloads_the_saved_state_format(self):
        state = AppState(
            people=[Person("p1", "A")],
            items=[Item("i1", "x", 100, "p1", ["p1"], {"mode": "equal"})],
        )
        self.ui._state = state
        self.status.textContent = "stale message"

        self.ui.on_export_state(None)

        body = self.ui.document.body
        self.assertEqual(len(body.appended), 1)
        link = body.appended[0]
        self.assertEqual(link.download, "bunnysplit-export.json")
        self.assertTrue(
            link.href.startswith("data:application/json;charset=utf-8,"))
        self.assertEqual(link.click_count, 1)
        self.assertEqual(body.children, [])  # removed after triggering download
        self.assertEqual(link.href.split(",", 1)[1],
                         self.ui._storage.dumps(state))
        self.assertEqual(self.status.textContent, "")

    def test_export_still_downloads_but_flags_a_bill_it_cannot_restore(self):
        # A bill saved before the text caps existed may exceed them.
        self.ui._state = AppState(people=[Person("p1", "n" * (MAX_NAME_LENGTH + 1))])

        self.ui.on_export_state(None)

        link = self.ui.document.body.appended[0]
        self.assertEqual(link.click_count, 1)
        self.assertIn("can't restore", self.status.textContent)
        self.assertIn("Names can be at most", self.status.textContent)
        self.assertEqual(self.status.className, "error")

    def test_export_flag_for_an_oversized_bill_does_not_contradict_it(self):
        # The file was just downloaded, so "too large to back up" would be
        # wrong; say how large the file is instead.
        self.ui._state = AppState(people=[
            Person("p%d" % i, "P%d" % i) for i in range(50)])
        size = len(self.ui._storage.dumps(self.ui._state).encode())
        self.assertTrue(1024 < size <= 2048, size)

        with mock.patch.object(self.ui._storage, "MAX_BACKUP_BYTES", 1024):
            self.ui.on_export_state(None)

        self.assertEqual(self.ui.document.body.appended[0].click_count, 1)
        self.assertEqual(self.status.textContent,
                         "Exported, but this app can't restore that file: "
                         "it is 2 KiB, over the 1 KiB limit.")
        self.assertEqual(self.status.className, "error")

    def test_import_with_an_over_long_name_is_rejected(self):
        original = AppState(people=[Person("p1", "Keep me")])
        self.ui._state = original

        self.import_text(json.dumps({
            "people": [{"id": "p1", "name": "n" * (MAX_NAME_LENGTH + 1)}],
            "items": []}))

        self.assertIs(self.ui._state, original)
        self.assertIsNone(self.saved())
        self.assertEqual(self.status.className, "error")

    def test_import_with_an_overflowing_weight_still_saves_loadable_json(self):
        # 1e309 parses to inf; saving it verbatim would write a bare
        # inf/Infinity that the next load cannot parse, emptying the bill.
        text = (
            '{"people": [{"id": "p1", "name": "A"}, {"id": "p2", "name": "B"}],'
            ' "items": [{"id": "i1", "description": "x", "amount_cents": 500,'
            ' "payer_id": "p1", "participant_ids": ["p1", "p2"], "split":'
            ' {"mode": "uneven", "weights": {"p1": 1e309, "p2": 1},'
            ' "junk": [1e309]}}]}')

        self.import_text(text)

        def reject(constant):
            raise ValueError("non-standard JSON constant " + constant)

        raw = self.local.values["bunnysplit"]
        json.loads(raw, parse_constant=reject)
        self.assertIn("fixed or skipped", self.status.textContent)
        reloaded = load_storage(FakeLocalStorage({"bunnysplit": raw}))
        state = reloaded.load()
        self.assertEqual(len(state.items), 1)
        self.assertEqual(reloaded.recovery_warning(), "")

    def test_weights_for_non_participants_are_reported_and_not_saved(self):
        # Every later page load parses the saved bill again, so keys the
        # split never reads must not be kept.
        weights = {"p1": 1, "p2": 3}
        weights.update(("x%d" % n, 1) for n in range(MAX_PEOPLE - 2))
        text = json.dumps({
            "people": [{"id": "p1", "name": "A"}, {"id": "p2", "name": "B"}],
            "items": [{"id": "i1", "description": "x", "amount_cents": 400,
                       "payer_id": "p1", "participant_ids": ["p1", "p2"],
                       "split": {"mode": "uneven", "weights": weights}}],
        })

        self.import_text(text)

        self.assertEqual(self.saved()["items"][0]["split"],
                         {"mode": "uneven", "weights": {"p1": 1, "p2": 3}})
        self.assertEqual(
            self.status.textContent,
            "Imported 2 people and 1 item. 1 problem was fixed or skipped; "
            "see the browser console for details.")

    def test_import_into_an_empty_bill_applies_without_asking(self):
        field = self.import_text(BACKUP)

        self.assertEqual([p.name for p in self.ui._state.people], ["A"])
        self.assertEqual(len(self.ui._state.items), 1)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.saved(), self.ui._state.to_dict())
        self.assertEqual(self.status.textContent,
                         "Imported 1 person and 1 item.")
        self.assertEqual(self.status.className, "status")
        self.assertEqual(field.value, "")  # cleared so re-picking refires change
        self.assertEqual(self.ui._next_id("p"), "p2")  # counter reseeded, no clash

    def test_import_over_an_existing_bill_replaces_it_after_confirmation(self):
        self.ui._state = AppState(people=[Person("old", "Stale")])

        self.import_text(BACKUP)

        self.assertEqual(len(self.prompts), 1)
        self.assertIn("current bill (1 person, 0 items)", self.prompts[0])
        self.assertIn("imported one (1 person, 1 item)", self.prompts[0])
        self.assertEqual([p.name for p in self.ui._state.people], ["A"])
        self.assertEqual(self.saved(), self.ui._state.to_dict())

    def test_declining_the_confirmation_keeps_the_current_bill(self):
        original = AppState(people=[Person("p1", "Keep me")])
        self.ui._state = original
        self.confirm_answer = False

        self.import_text(BACKUP)

        self.assertIs(self.ui._state, original)
        self.assertIsNone(self.saved())
        self.assertIn("cancelled", self.status.textContent)
        self.assertEqual(self.status.className, "status")

    def test_json_that_is_not_a_backup_is_rejected_without_touching_the_bill(self):
        original = AppState(people=[Person("p1", "Keep me")])
        for text in ("{}", json.dumps({"name": "pkg", "version": "1.0"}),
                     "[1, 2, 3]"):
            with self.subTest(text=text):
                self.ui._state = original

                self.import_text(text)

                self.assertIs(self.ui._state, original)
                self.assertEqual(self.prompts, [])
                self.assertIsNone(self.saved())
                self.assertIn("isn't a bunnysplit backup",
                              self.status.textContent)
                self.assertEqual(self.status.className, "error")

    def test_invalid_json_is_rejected_without_touching_the_bill(self):
        original = AppState(people=[Person("p1", "Keep me")])
        self.ui._state = original

        self.import_text("{not json")

        self.assertIs(self.ui._state, original)
        self.assertIsNone(self.saved())
        self.assertEqual(self.status.textContent, "That file isn't valid JSON.")
        self.assertEqual(self.status.className, "error")

    def test_partial_recovery_is_flagged_before_and_after_import(self):
        self.ui._state = AppState(people=[Person("old", "Stale")])
        text = json.dumps({
            "people": [
                {"id": "p1", "name": "A"},
                {"id": "p1", "name": "duplicate"},
            ],
            "items": [],
        })

        self.import_text(text)

        self.assertIn(
            "\n\n1 problem was fixed or skipped while reading the file.",
            self.prompts[0])
        self.assertEqual([p.name for p in self.ui._state.people], ["A"])
        self.assertIn("1 problem was fixed or skipped; see the browser console",
                      self.status.textContent)

    def test_the_note_counts_problems_not_records(self):
        # One item can have several problems; calling each a "record" would
        # claim more of the file was damaged than it holds.
        self.ui._state = AppState(people=[Person("old", "Stale")])
        text = json.dumps({
            "people": [{"id": "p1", "name": "A"}],
            "items": [{"id": "i1", "description": "x", "amount_cents": "5",
                       "payer_id": "p9", "participant_ids": ["p1"],
                       "split": {"mode": "equal"}}],
        })

        self.import_text(text)

        self.assertIn(
            "\n\n2 problems were fixed or skipped while reading the file.",
            self.prompts[0])
        self.assertEqual(
            self.status.textContent,
            "Imported 1 person and 0 items. 2 problems were fixed or skipped;"
            " see the browser console for details.")

    def test_oversized_file_is_rejected_before_reading(self):
        big = FakeFile(text_value=BACKUP,
                       size=self.ui._storage.MAX_BACKUP_BYTES + 1)

        self.import_file(big)

        self.assertEqual(big.promises, [])
        self.assertEqual(self.ui._state.people, [])
        self.assertIn("too large", self.status.textContent)
        self.assertEqual(self.status.className, "error")

    def test_wrapped_negative_size_is_rejected_before_reading(self):
        big = FakeFile(text_value=BACKUP, size=-2147483648)

        self.import_file(big)

        self.assertEqual(big.promises, [])
        self.assertIn("too large", self.status.textContent)
        self.assertEqual(self.status.className, "error")

    def test_read_is_capped_even_when_reported_size_is_wrong(self):
        # A 4 GiB + 5 B file reports size 5 through int32 truncation.
        limit = self.ui._storage.MAX_BACKUP_BYTES
        huge = FakeFile(text_value="x" * (limit + 10), size=5)

        self.import_file(huge)

        self.assertEqual(huge.slices, [(0, limit + 1)])
        self.assertEqual(self.ui._state.people, [])
        self.assertIn("too large", self.status.textContent)
        self.assertEqual(self.status.className, "error")

    def test_empty_backup_over_an_existing_bill_still_asks_first(self):
        original = AppState(people=[Person("p1", "Keep me")])
        self.ui._state = original
        self.confirm_answer = False

        self.import_text(json.dumps({"people": [], "items": []}))

        self.assertEqual(len(self.prompts), 1)
        self.assertIn("imported one (0 people, 0 items)", self.prompts[0])
        self.assertIs(self.ui._state, original)
        self.assertIsNone(self.saved())

    def test_unreadable_file_is_reported(self):
        field = self.import_file(FakeFile(error=Exception("NotReadableError")))

        self.assertEqual(self.status.textContent,
                         "Could not read the selected file.")
        self.assertEqual(self.status.className, "error")
        self.assertEqual(field.value, "")

    def test_a_read_rejection_logs_the_browser_error_itself(self):
        # Under MicroPython, str() of the DOMException that rejects
        # Blob.text() is only "<JsProxy n>", losing its name and message.
        error = FakeJsProxy("NotReadableError")

        self.import_file(FakeFile(error=error))

        self.assertEqual(self.status.textContent,
                         "Could not read the selected file.")
        self.assertEqual(self.status.className, "error")
        self.assertIn(("bunnysplit: could not read import:", error),
                      self.ui.window.console.calls)
        self.assertFalse(any(isinstance(part, str) and "<JsProxy" in part
                             for call in self.ui.window.console.calls
                             for part in call))

    def test_a_python_error_before_reading_is_logged_as_text(self):
        # A Python exception passed to JS as its own argument is an opaque
        # PyProxy there, so it is logged as text instead.
        no_slice = types.SimpleNamespace(size=10)

        self.import_file(no_slice)

        self.assertEqual(self.status.textContent,
                         "Could not read the selected file.")
        self.assertEqual(self.status.className, "error")
        self.assertTrue(any(w.startswith("bunnysplit: could not read import: ")
                            and "slice" in w
                            for w in self.ui.window.console.warnings))
        self.assertFalse(any(isinstance(part, BaseException)
                             for call in self.ui.window.console.calls
                             for part in call))

    def test_unexpected_import_failure_is_reported_not_raised(self):
        original = AppState(people=[Person("p1", "Keep me")])
        self.ui._state = original

        def explode(text):
            raise RuntimeError("boom")

        self.ui._storage.parse_backup = explode

        self.import_text(BACKUP)

        self.assertIs(self.ui._state, original)
        self.assertIn("Import failed", self.status.textContent)
        self.assertEqual(self.status.className, "error")

    def test_imports_share_one_long_lived_set_of_promise_callbacks(self):
        # The read promise settles after the change handler returns, so the
        # callbacks can't be render-scoped. Per-import proxies would either
        # leak or be destroyed while still executing; reuse one set instead.
        created = []
        wrap = self.ui.create_proxy
        self.ui.create_proxy = lambda fn: created.append(fn) or wrap(fn)
        first = FakeFile(text_value=BACKUP)
        second = FakeFile(text_value=BACKUP)

        self.import_file(first)
        created_by_first = len(created)
        self.import_file(second)

        self.assertEqual(len(created), created_by_first)
        self.assertEqual(first.promises[0].callbacks,
                         second.promises[0].callbacks)
        self.assertIsNotNone(first.promises[0].catch_callback)
        self.assertEqual(first.promises[0].catch_callback,
                         second.promises[0].catch_callback)

    def fail_saves(self, error):
        def save(state, text=None):
            raise error

        self.ui._storage.save = save

    def test_a_failed_save_keeps_the_current_bill(self):
        original = AppState(people=[Person("old", "Keep")])
        self.ui._state = original
        renders = []
        self.ui.render_all = lambda: renders.append(1)
        self.fail_saves(RuntimeError("QuotaExceededError"))

        self.import_text(BACKUP)

        self.assertIs(self.ui._state, original)
        self.assertIsNone(self.saved())
        self.assertEqual(renders, [])
        self.assertEqual(
            self.status.textContent,
            "Could not save the imported bill; your current bill was kept.")
        self.assertEqual(self.status.className, "error")

    def test_a_save_error_that_skips_python_handlers_keeps_the_bill(self):
        # Under MicroPython a JS exception from localStorage.setItem unwinds
        # through Python without running except or finally, so the bill must
        # not be swapped in before the save returns, and the failure message
        # must already be showing when it escapes.
        original = AppState(people=[Person("old", "Keep")])
        self.ui._state = original
        self.fail_saves(FakeJsError("QuotaExceededError"))

        with self.assertRaises(FakeJsError):
            self.ui._import_backup(BACKUP)

        self.assertIs(self.ui._state, original)
        self.assertEqual(
            self.status.textContent,
            "Could not save the imported bill; your current bill was kept.")
        self.assertEqual(self.status.className, "error")

    def test_the_read_promise_chain_reports_errors_that_escape(self):
        # A trailing catch on the read promise sees what escaped the text
        # callback. It keeps a specific message that is already showing...
        self.ui._state = AppState(people=[Person("old", "Keep")])
        self.fail_saves(FakeJsError("QuotaExceededError"))

        self.import_text(BACKUP)

        self.assertEqual(
            self.status.textContent,
            "Could not save the imported bill; your current bill was kept.")
        self.assertTrue(any("QuotaExceededError" in w
                            for w in self.ui.window.console.warnings))

        # ...and otherwise says the import failed.
        def explode(text):
            raise FakeJsError("RangeError")

        self.ui._storage.parse_backup = explode

        self.import_text(BACKUP)

        self.assertEqual(self.status.textContent,
                         "Import failed; see the browser console for details.")
        self.assertEqual(self.status.className, "error")


class BillLimitTests(unittest.TestCase):
    """The UI stops at the same limits import enforces, so every bill the
    app can build can also be restored from its own backup."""

    def setUp(self):
        self.people_error = FakeElement()
        self.item_error = FakeElement()
        self.name_field = FakeElement()
        self.desc = FakeElement()
        self.amount = FakeElement()
        self.payer = FakeElement()
        self.uneven = types.SimpleNamespace(checked=False)
        self.checks = []
        self.weight_fields = []
        self.ui = load_ui(FakePill(), {
            "#people-error": self.people_error,
            "#item-error": self.item_error,
            "#person-name": self.name_field,
            "#item-desc": self.desc,
            "#item-amount": self.amount,
            "#payer-select": self.payer,
            "#mode-uneven": self.uneven,
        }, {".p-check": self.checks, ".p-weight": self.weight_fields})
        self.ui._storage = load_storage(FakeLocalStorage())

    def people(self, n):
        return [Person("p%d" % i, "P%d" % i) for i in range(n)]

    def fill_item_form(self, participant_ids):
        self.desc.value = "Lunch"
        self.amount.value = "12.00"
        self.payer.value = participant_ids[0]
        self.checks[:] = [types.SimpleNamespace(checked=True, value=pid)
                          for pid in participant_ids]

    def test_adding_a_person_stops_at_the_people_limit(self):
        self.ui._state = AppState(people=self.people(MAX_PEOPLE - 1))
        self.name_field.value = "Last one"
        self.ui.on_add_person(None)
        self.assertEqual(len(self.ui._state.people), MAX_PEOPLE)

        self.name_field.value = "One too many"
        self.ui.on_add_person(None)

        self.assertEqual(len(self.ui._state.people), MAX_PEOPLE)
        self.assertEqual(self.people_error.textContent,
                         "A bill can have at most %d people." % MAX_PEOPLE)

    def test_adding_an_item_stops_at_the_item_limit(self):
        people = self.people(1)
        items = [Item("i%d" % i, "x", 100, "p0", ["p0"], {"mode": "equal"})
                 for i in range(MAX_ITEMS)]
        self.ui._state = AppState(people=people, items=items)
        self.fill_item_form(["p0"])

        self.ui.on_add_item(None)

        self.assertEqual(len(self.ui._state.items), MAX_ITEMS)
        self.assertEqual(self.item_error.textContent,
                         "A bill can have at most %d items." % MAX_ITEMS)

    def test_adding_an_item_stops_at_the_participant_entry_limit(self):
        per_item = MAX_PARTICIPANT_REFS // (MAX_ITEMS - 1) + 1
        people = self.people(per_item)
        pids = [p.id for p in people]
        # Fill up to exactly the limit with fewer than MAX_ITEMS items.
        items, refs = [], 0
        while refs + per_item <= MAX_PARTICIPANT_REFS:
            items.append(Item("i%d" % len(items), "x", 100, "p0", pids,
                              {"mode": "equal"}))
            refs += per_item
        tail = MAX_PARTICIPANT_REFS - refs
        if tail:
            items.append(Item("i%d" % len(items), "x", 100, "p0", pids[:tail],
                              {"mode": "equal"}))
        self.assertLess(len(items), MAX_ITEMS)
        self.ui._state = AppState(people=people, items=items)
        self.fill_item_form(["p0"])

        self.ui.on_add_item(None)

        self.assertEqual(len(self.ui._state.items), len(items))
        self.assertEqual(
            self.item_error.textContent,
            "A bill can have at most %d participant entries across all items."
            % MAX_PARTICIPANT_REFS)

    def test_adding_a_person_with_an_over_long_name_is_rejected(self):
        self.ui._state = AppState()
        self.name_field.value = "n" * (MAX_NAME_LENGTH + 1)

        self.ui.on_add_person(None)

        self.assertEqual(self.ui._state.people, [])
        self.assertEqual(self.people_error.textContent,
                         "Names can be at most %d characters." % MAX_NAME_LENGTH)
        self.assertEqual(self.name_field.value, "n" * (MAX_NAME_LENGTH + 1))

    def test_adding_an_item_with_an_over_long_description_is_rejected(self):
        self.ui._state = AppState(people=self.people(1))
        self.fill_item_form(["p0"])
        self.desc.value = "d" * (MAX_DESCRIPTION_LENGTH + 1)

        self.ui.on_add_item(None)

        self.assertEqual(self.ui._state.items, [])
        self.assertEqual(
            self.item_error.textContent,
            "Descriptions can be at most %d characters." % MAX_DESCRIPTION_LENGTH)

    def test_adding_an_item_that_would_make_the_backup_too_large_is_rejected(self):
        self.ui._state = AppState(people=self.people(1))
        storage = self.ui._storage
        limit = len(storage.dumps(self.ui._state).encode()) + 10
        self.fill_item_form(["p0"])

        with mock.patch.object(storage, "MAX_BACKUP_BYTES", limit):
            self.ui.on_add_item(None)

        self.assertEqual(self.ui._state.items, [])
        self.assertIn("too large to back up", self.item_error.textContent)
        self.assertEqual(self.desc.value, "Lunch")  # form kept for editing

    def test_a_legacy_bill_with_over_long_text_can_still_grow(self):
        # Only the new record's text is checked when adding, so a bill saved
        # before the caps existed is not stuck.
        self.ui._state = AppState(
            people=[Person("old", "n" * (MAX_NAME_LENGTH + 5))])
        self.name_field.value = "Bo"

        self.ui.on_add_person(None)

        self.assertEqual([p.name for p in self.ui._state.people][1:], ["Bo"])
        self.assertEqual(self.people_error.textContent, "")

    def test_each_add_serializes_the_bill_once(self):
        # dumps() is quadratic in output size under MicroPython, so the size
        # check's serialization is reused for the save.
        self.ui._state = AppState()
        self.name_field.value = "Bo"
        with mock.patch.object(self.ui._storage, "dumps",
                               wraps=self.ui._storage.dumps) as dumps:
            self.ui.on_add_person(None)

        self.assertEqual(dumps.call_count, 1)

    def fill_weights(self, weights):
        self.uneven.checked = True
        self.weight_fields[:] = [
            types.SimpleNamespace(value=value,
                                  getAttribute=lambda name, pid=pid: pid)
            for pid, value in weights.items()]

    def test_adding_an_item_with_a_weight_above_the_cap_is_rejected(self):
        # split_item() would cap it, and a backup of it would be reported
        # as changed on import.
        self.ui._state = AppState(people=self.people(2))
        self.fill_item_form(["p0", "p1"])
        self.fill_weights({"p0": "1", "p1": "1e13"})

        self.ui.on_add_item(None)

        self.assertEqual(self.ui._state.items, [])
        self.assertEqual(self.item_error.textContent,
                         "Weights are unreasonably large.")
        self.assertEqual(self.desc.value, "Lunch")  # form kept for editing

    def test_a_weight_at_the_cap_is_added_and_restores_cleanly(self):
        self.ui._state = AppState(people=self.people(2))
        self.fill_item_form(["p0", "p1"])
        self.fill_weights({"p0": "1", "p1": "1000000000000"})

        self.ui.on_add_item(None)

        self.assertEqual(self.item_error.textContent, "")
        storage = self.ui._storage
        restored, issues = storage.parse_backup(storage.dumps(self.ui._state))
        self.assertEqual(restored.items[0].weights(),
                         {"p0": 1.0, "p1": MAX_WEIGHT})
        self.assertEqual(issues, [])

    def test_adding_an_item_below_the_limits_still_works(self):
        self.ui._state = AppState(people=self.people(2))
        self.fill_item_form(["p0", "p1"])

        self.ui.on_add_item(None)

        self.assertEqual(len(self.ui._state.items), 1)
        self.assertEqual(self.item_error.textContent, "")


if __name__ == "__main__":
    unittest.main()
