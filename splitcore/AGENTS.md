# AGENTS.md

- `splitcore/` is the pure logic layer. Keep it browser-free and importable under both CPython and MicroPython.
- Preserve exact penny conservation: every split path must sum exactly to `amount_cents`.
- Money stays as integer cents; formatting belongs in `ui.py`, not here.
- `model.py` is the persisted-state trust boundary. Harden malformed saved data here rather than making the UI or calc layer compensate later.
- Keep `MODE_EQUAL` and `MODE_UNEVEN` literal values stable; tests lock persisted JSON to those exact strings.
- Do not switch `model.py` to dataclasses.
- `AppState.from_dict()` is the persisted/untrusted-state normalization path. Direct `Person`, `Item`, and `AppState` constructors do not enforce referential integrity, so do not pass browser/persisted input to them without equivalent validation.
- Person and item IDs are logical keys: keep them non-empty, bounded, and unique. Duplicate IDs collapse dictionary-backed shares and make UI removal affect multiple records.
- Bound digit counts before `parse_cents()` calls `int()`; a later `MAX_CENTS` comparison does not prevent oversized conversion failures.
- After edits here, run `python -m unittest discover -s tests` and `python -m py_compile splitcore/*.py`.
- Persisted-state hardening caps amounts (`MAX_CENTS` in `model.py`) and uneven weights (`_MAX_WEIGHT` in `calc.py`); preserve those bounds and exact penny conservation when changing split math or deserialization.
- `Item.from_dict()` keeps only `mode` (and `weights` for uneven splits) in `split`, and only finite `int`/`float` weights (not `bool`); strings, `null`, booleans, non-finite numbers and containers are dropped with an issue. `1e309` parses to `inf`, and `json.dumps` writes it back as a bare `inf`/`Infinity` that the next `json.loads` rejects, losing the whole bill. The app only ever saves float weights; an imported long digit-string weight froze every render, because MicroPython's `int()` of it is quadratic.
- Never `str()` a container from untrusted JSON: under MicroPython on wasm, `str()` of a deeply nested list overflows the JS stack with a `RangeError` that no Python `except` catches. A person whose name is a list or object is skipped; such an item description is blanked and the item kept. Report an issue whenever a field that affects money or the roster is sanitized or defaulted (a missing `amount_cents` included): backup import only warns before replacing a bill when issues are reported.
- If repository behavior contradicts this file, patch `splitcore/AGENTS.md` in the same change and preserve the `splitcore/CLAUDE.md` symlink.
