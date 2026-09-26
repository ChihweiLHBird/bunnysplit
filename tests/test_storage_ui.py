"""Browser-bound persistence and save-status tests using small PyScript fakes."""

import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest import mock

from splitcore.model import AppState, Item, Person


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
    def __init__(self, extra_elements=None):
        self.body = FakeElement("body")
        self._extra = extra_elements or {}

    def querySelector(self, selector):
        return self._extra.get(selector)

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


def load_ui(pill, extra_elements=None):
    elements = dict(extra_elements or {})
    elements[".saved-pill"] = pill
    pyscript = types.ModuleType("pyscript")
    pyscript.document = FakeDocument(elements)
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


if __name__ == "__main__":
    unittest.main()
