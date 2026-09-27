"""Browser-bound persistence and save-status tests using small PyScript fakes."""

import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest import mock

from splitcore.model import (
    MAX_DESCRIPTION_LENGTH, MAX_ITEMS, MAX_NAME_LENGTH, MAX_PARTICIPANT_REFS,
    MAX_PEOPLE, AppState, Item, Person)


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

    def warn(self, message):
        self.warnings.append(message)

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
        pids = ["p%d" % i for i in range(MAX_PARTICIPANT_REFS // 10 + 1)]
        items = [{"id": "i%d" % i, "participant_ids": pids} for i in range(10)]

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

        self.assertIn("too large to back up", str(ctx.exception))

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


class FakePromise:
    def __init__(self, value=None, error=None):
        self._value = value
        self._error = error
        self.callbacks = None

    def then(self, on_ok, on_err):
        # Settles synchronously; a browser settles after the change handler
        # returns, so import callbacks must not depend on either ordering.
        self.callbacks = (on_ok, on_err)
        if self._error is not None:
            on_err(self._error)
        else:
            on_ok(self._value)


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
        self.assertIn("skipped or adjusted", self.status.textContent)
        reloaded = load_storage(FakeLocalStorage({"bunnysplit": raw}))
        state = reloaded.load()
        self.assertEqual(len(state.items), 1)
        self.assertEqual(reloaded.recovery_warning(), "")

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

        self.assertIn("1 record", self.prompts[0])
        self.assertEqual([p.name for p in self.ui._state.people], ["A"])
        self.assertIn("1 record was skipped or adjusted",
                      self.status.textContent)

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

    def test_imports_share_one_long_lived_pair_of_promise_callbacks(self):
        # The read promise settles after the change handler returns, so the
        # callbacks can't be render-scoped. Per-import proxies would either
        # leak or be destroyed while still executing; reuse one pair instead.
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
        self.ui = load_ui(FakePill(), {
            "#people-error": self.people_error,
            "#item-error": self.item_error,
            "#person-name": self.name_field,
            "#item-desc": self.desc,
            "#item-amount": self.amount,
            "#payer-select": self.payer,
            "#mode-uneven": self.uneven,
        }, {".p-check": self.checks})
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

    def test_adding_an_item_below_the_limits_still_works(self):
        self.ui._state = AppState(people=self.people(2))
        self.fill_item_form(["p0", "p1"])

        self.ui.on_add_item(None)

        self.assertEqual(len(self.ui._state.items), 1)
        self.assertEqual(self.item_error.textContent, "")


if __name__ == "__main__":
    unittest.main()
