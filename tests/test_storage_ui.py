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

    def then(self, on_ok, on_err):
        if self._error is not None:
            on_err(self._error)
        else:
            on_ok(self._value)


class FakeFile:
    def __init__(self, text_value=None, error=None):
        self._text_value = text_value
        self._error = error

    def text(self):
        return FakePromise(self._text_value, self._error)


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


class ImportExportTests(unittest.TestCase):
    def test_export_state_builds_a_downloadable_json_link(self):
        ui = load_ui(FakePill())
        ui._state = AppState(
            people=[Person("p1", "A")],
            items=[Item("i1", "x", 100, "p1", ["p1"], {"mode": "equal"})],
        )

        ui.on_export_state(None)

        body = ui.document.body
        self.assertEqual(len(body.appended), 1)
        link = body.appended[0]
        self.assertEqual(link.download, "bunnysplit-export.json")
        self.assertTrue(
            link.href.startswith("data:application/json;charset=utf-8,"))
        self.assertEqual(link.click_count, 1)
        self.assertEqual(body.children, [])  # removed after triggering download
        payload = json.loads(link.href.split(",", 1)[1])
        self.assertEqual(payload, ui._state.to_dict())

    def test_import_replaces_state_and_resets_id_counter(self):
        data_error = FakeElement()
        ui = load_ui(FakePill(), {"#data-error": data_error})
        ui._state = AppState(people=[Person("old", "Stale")])
        payload = json.dumps({
            "people": [{"id": "p1", "name": "A"}],
            "items": [{
                "id": "i1", "description": "x", "amount_cents": 500,
                "payer_id": "p1", "participant_ids": ["p1"],
                "split": {"mode": "equal"},
            }],
        })
        field = FakeFileInput([FakeFile(text_value=payload)])
        event = types.SimpleNamespace(target=field)

        ui.on_import_file_change(event)

        self.assertEqual([p.name for p in ui._state.people], ["A"])
        self.assertEqual(len(ui._state.items), 1)
        self.assertEqual(data_error.textContent, "Imported successfully.")
        self.assertEqual(field.value, "")  # cleared so re-picking refires change
        self.assertEqual(ui._next_id("p"), "p2")  # counter reseeded, no clash

    def test_import_invalid_json_reports_error_without_touching_state(self):
        data_error = FakeElement()
        ui = load_ui(FakePill(), {"#data-error": data_error})
        original = AppState(people=[Person("p1", "Keep me")])
        ui._state = original
        field = FakeFileInput([FakeFile(text_value="{not json")])
        event = types.SimpleNamespace(target=field)

        ui.on_import_file_change(event)

        self.assertIs(ui._state, original)
        self.assertEqual(data_error.textContent, "That file isn't valid JSON.")

    def test_import_partial_recovery_reports_issue_count(self):
        data_error = FakeElement()
        ui = load_ui(FakePill(), {"#data-error": data_error})
        ui._state = AppState()
        payload = json.dumps({
            "people": [
                {"id": "p1", "name": "A"},
                {"id": "p1", "name": "duplicate"},
            ],
            "items": [],
        })
        field = FakeFileInput([FakeFile(text_value=payload)])
        event = types.SimpleNamespace(target=field)

        ui.on_import_file_change(event)

        self.assertEqual([p.name for p in ui._state.people], ["A"])
        self.assertIn("Imported with 1 issue(s)", data_error.textContent)


if __name__ == "__main__":
    unittest.main()
