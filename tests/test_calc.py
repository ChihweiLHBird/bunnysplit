"""Tests for splitcore. Stdlib unittest so it runs with no pip install:

    python3 -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from splitcore.calc import (
    MAX_AMOUNT_INPUT_LENGTH,
    is_finite_number,
    parse_cents,
    parse_finite,
    per_person_totals,
    settle_up,
    split_item,
)
from splitcore.model import (
    MAX_CENTS,
    MAX_ID_LENGTH,
    MODE_EQUAL,
    MODE_UNEVEN,
    AppState,
    Item,
    Person,
)


def item(amount, participants, payer="a", split=None, iid="i1", desc="x"):
    return Item(iid, desc, amount, payer, participants,
                split or {"mode": MODE_EQUAL})


def uneven(weights):
    return {"mode": MODE_UNEVEN, "weights": weights}


class SplitItemTests(unittest.TestCase):
    def test_equal_exact(self):
        shares = split_item(item(900, ["a", "b", "c"]))
        self.assertEqual(shares, {"a": 300, "b": 300, "c": 300})

    def test_equal_penny_remainder_sums_exactly(self):
        shares = split_item(item(1000, ["a", "b", "c"]))
        # 1000 / 3 -> first participants absorb the leftover penny.
        self.assertEqual(shares, {"a": 334, "b": 333, "c": 333})
        self.assertEqual(sum(shares.values()), 1000)

    def test_uneven_proportional_with_remainder(self):
        shares = split_item(item(1000, ["a", "b"],
                                  split=uneven({"a": 1, "b": 3})))
        self.assertEqual(shares, {"a": 250, "b": 750})
        self.assertEqual(sum(shares.values()), 1000)

    def test_uneven_remainder_goes_to_largest_fraction(self):
        # 100 cents, weights 1:1:1 -> 33.33 each, 1 penny remainder.
        # Equal fractions tie-break by participant order -> "a" gets it.
        shares = split_item(item(100, ["a", "b", "c"],
                                 split=uneven({"a": 1, "b": 1, "c": 1})))
        self.assertEqual(shares, {"a": 34, "b": 33, "c": 33})
        self.assertEqual(sum(shares.values()), 100)

    def test_uneven_all_zero_weights_falls_back_to_equal(self):
        shares = split_item(item(300, ["a", "b", "c"],
                                 split=uneven({"a": 0, "b": 0, "c": 0})))
        self.assertEqual(shares, {"a": 100, "b": 100, "c": 100})
        self.assertEqual(sum(shares.values()), 300)

    def test_no_participants(self):
        self.assertEqual(split_item(item(500, [])), {})

    def test_unknown_split_mode_raises(self):
        it = Item("i1", "x", 100, "a", ["a", "b"], {"mode": "weird"})
        with self.assertRaises(ValueError):
            split_item(it)

    def test_negative_amount_is_conserved_not_crash(self):
        # The pure layer does not validate sign (UI does). Document that a
        # negative amount still distributes exactly and never crashes.
        shares = split_item(item(-300, ["a", "b", "c"]))
        self.assertEqual(shares, {"a": -100, "b": -100, "c": -100})
        self.assertEqual(sum(shares.values()), -300)


class TotalsTests(unittest.TestCase):
    def test_totals_across_multiple_items(self):
        state = AppState(
            people=[Person("a", "A"), Person("b", "B")],
            items=[
                item(1000, ["a", "b"], payer="a", iid="1"),
                item(500, ["b"], payer="b", iid="2"),
            ],
        )
        self.assertEqual(per_person_totals(state), {"a": 500, "b": 1000})

    def test_empty_state(self):
        self.assertEqual(per_person_totals(AppState()), {})
        self.assertEqual(settle_up(AppState()), [])


class SettleUpTests(unittest.TestCase):
    def test_multi_payer_exact_transfers(self):
        # A pays 900 split A,B,C ; C pays 600 split B,C.
        # owed a:300 b:600 c:600 ; paid a:900 c:600 ; net a:+600 b:-600 c:0.
        state = AppState(
            people=[Person("a", "A"), Person("b", "B"), Person("c", "C")],
            items=[
                item(900, ["a", "b", "c"], payer="a", iid="1"),
                item(600, ["b", "c"], payer="c", iid="2"),
            ],
        )
        self.assertEqual(settle_up(state), [("b", "a", 600)])

    def test_greedy_one_creditor_many_debtors(self):
        # A fronts 400 for four; each owes 100, A net +300.
        state = AppState(
            people=[Person("a", "A"), Person("b", "B"),
                    Person("c", "C"), Person("d", "D")],
            items=[item(400, ["a", "b", "c", "d"], payer="a", iid="1")],
        )
        self.assertEqual(
            settle_up(state),
            [("b", "a", 100), ("c", "a", 100), ("d", "a", 100)],
        )

    def test_transfers_conserve_and_are_positive(self):
        state = AppState(
            people=[Person("a", "A"), Person("b", "B"), Person("c", "C")],
            items=[
                item(1000, ["a", "b", "c"], payer="a", iid="1"),
                item(330, ["a", "c"], payer="b", iid="2"),
            ],
        )
        owed = per_person_totals(state)
        paid = {"a": 1000, "b": 330, "c": 0}
        net = {p.id: paid[p.id] - owed[p.id] for p in state.people}
        for debtor, creditor, amount in settle_up(state):
            self.assertGreater(amount, 0)
            net[debtor] += amount
            net[creditor] -= amount
        self.assertEqual(net, {"a": 0, "b": 0, "c": 0})


class ReferentialIntegrityTests(unittest.TestCase):
    """Characterizes the known limitation (security review, Medium): ids
    referencing non-people are not crashes but are silently unsettled.
    These tests lock the behavior so a change is deliberate."""

    def test_unknown_participant_counted_but_not_settled(self):
        state = AppState(
            people=[Person("a", "A"), Person("b", "B")],
            items=[item(300, ["a", "b", "ghost"], payer="a", iid="i1")],
        )
        self.assertEqual(per_person_totals(state),
                         {"a": 100, "b": 100, "ghost": 100})
        # ghost is excluded from net; only b settles with a.
        self.assertEqual(settle_up(state), [("b", "a", 100)])

    def test_unknown_payer_payment_is_lost(self):
        state = AppState(
            people=[Person("a", "A"), Person("b", "B")],
            items=[item(200, ["a", "b"], payer="ghost", iid="i1")],
        )
        # Nobody among people is a creditor -> no transfers.
        self.assertEqual(settle_up(state), [])


class ModelTests(unittest.TestCase):
    def test_person_by_id(self):
        p = Person("a", "Alice")
        state = AppState(people=[p, Person("b", "Bob")])
        self.assertIs(state.person_by_id("a"), p)
        self.assertIsNone(state.person_by_id("missing"))

    def test_items_referencing_payer_and_participant(self):
        state = AppState(
            people=[Person("a", "A"), Person("b", "B"), Person("c", "C")],
            items=[
                item(100, ["b", "c"], payer="a", iid="i1"),
                item(50, ["c"], payer="c", iid="i2"),
            ],
        )
        self.assertEqual([i.id for i in state.items_referencing("a")],
                         ["i1"])                       # payer only
        self.assertEqual([i.id for i in state.items_referencing("b")],
                         ["i1"])                       # participant only
        self.assertEqual({i.id for i in state.items_referencing("c")},
                         {"i1", "i2"})                 # both
        self.assertEqual(state.items_referencing("nobody"), [])

    def test_mode_constants_match_wire_values(self):
        # The app and persisted JSON depend on these literal values; lock
        # them so a constant rename can't silently diverge from old data.
        self.assertEqual((MODE_EQUAL, MODE_UNEVEN), ("equal", "uneven"))


class SerializationTests(unittest.TestCase):
    def test_round_trip(self):
        state = AppState(
            people=[Person("a", "Alice"), Person("b", "Bob")],
            items=[item(1234, ["a", "b"], payer="a",
                        split=uneven({"a": 2, "b": 1}))],
        )
        restored = AppState.from_dict(state.to_dict())
        self.assertEqual(restored.to_dict(), state.to_dict())

    def test_json_round_trip_float_weights(self):
        # Real uneven input yields float weights; exercise the actual
        # JSON path storage.py uses (json.dumps -> json.loads).
        state = AppState(
            people=[Person("a", "A"), Person("b", "B")],
            items=[item(1000, ["a", "b"], payer="a",
                        split=uneven({"a": 2.5, "b": 0.5}))],
        )
        restored = AppState.from_dict(json.loads(json.dumps(state.to_dict())))
        self.assertEqual(restored.to_dict(), state.to_dict())
        self.assertEqual(sum(split_item(restored.items[0]).values()), 1000)


class ConservationTests(unittest.TestCase):
    def test_money_is_conserved_across_amounts(self):
        for amt in (1, 2, 3, 7, 99, 100, 101, 1234, 99999, 100000):
            eq = split_item(item(amt, ["a", "b", "c"]))
            self.assertEqual(sum(eq.values()), amt, "equal %d" % amt)
            un = split_item(item(amt, ["a", "b", "c"],
                                 split=uneven({"a": 1, "b": 2, "c": 4})))
            self.assertEqual(sum(un.values()), amt, "uneven %d" % amt)


class HardeningTests(unittest.TestCase):
    """Untrusted input from typed fields or hand-edited localStorage must
    never crash or silently lose money."""

    def test_is_finite_number(self):
        for good in ("12", "0.5", 3, -2, "1e6"):
            self.assertTrue(is_finite_number(good))
        for bad in ("inf", "-inf", "nan", "1e400", "abc", "", None, "1,5"):
            self.assertFalse(is_finite_number(bad))

    def test_parse_finite_returns_value_or_none(self):
        self.assertEqual(parse_finite("12.50"), 12.5)
        self.assertEqual(parse_finite(-3), -3.0)
        for bad in ("inf", "nan", "1e400", "abc", "", None):
            self.assertIsNone(parse_finite(bad))

    def test_parse_cents_exact_no_float_drift(self):
        # The canonical IEEE-754 cents bug: int(round(float('0.295')*100))
        # gives 29, losing a half-cent. parse_cents must return 29 only by
        # truncation policy (3rd decimal dropped), NOT by float drift.
        self.assertEqual(parse_cents("0.295"), 29)
        self.assertEqual(parse_cents("0.29"), 29)
        self.assertEqual(parse_cents("1.005"), 100)  # truncates, never drifts
        self.assertEqual(parse_cents("12"), 1200)
        self.assertEqual(parse_cents("12.5"), 1250)
        self.assertEqual(parse_cents("12.50"), 1250)
        self.assertEqual(parse_cents("0.5"), 50)
        self.assertEqual(parse_cents(".5"), 50)
        self.assertEqual(parse_cents("-3.14"), -314)
        self.assertEqual(parse_cents("  12.34  "), 1234)

    def test_parse_cents_rejects(self):
        for bad in ("", "   ", ".", "abc", "1,5", "1.2.3", "1e3",
                    "inf", "nan", "+-1", None):
            self.assertIsNone(parse_cents(bad), bad)

    def test_parse_cents_rejects_oversized_digits_before_conversion(self):
        self.assertIsNone(parse_cents("1" * 5000))
        self.assertIsNone(parse_cents("1." + "2" * 5000))
        self.assertEqual(
            parse_cents("%d.00" % (MAX_CENTS // 100)),
            MAX_CENTS,
        )
        self.assertEqual(MAX_AMOUNT_INPUT_LENGTH, 14)

    def test_uneven_inf_weight_falls_back_not_crash(self):
        shares = split_item(item(900, ["a", "b", "c"],
                                 split=uneven({"a": "inf", "b": 1, "c": 1})))
        # inf weight sanitized to 0; the rest split the amount.
        self.assertEqual(shares, {"a": 0, "b": 450, "c": 450})
        self.assertEqual(sum(shares.values()), 900)

    def test_uneven_nan_and_negative_weights_sanitized(self):
        shares = split_item(item(600, ["a", "b"],
                                 split=uneven({"a": "nan", "b": -5})))
        # both invalid -> all zero -> equal-split fallback
        self.assertEqual(shares, {"a": 300, "b": 300})
        self.assertEqual(sum(shares.values()), 600)

    def test_non_dict_split_treated_as_equal(self):
        it = Item("i1", "x", 300, "a", ["a", "b", "c"], None)
        self.assertEqual(split_item(it), {"a": 100, "b": 100, "c": 100})

    def test_from_dict_skips_bad_record_and_coerces(self):
        # Item is given valid people refs so the orphan filter doesn't
        # drop it; this test stays focused on type-coercion behavior.
        raw = {
            "people": [
                {"id": 1, "name": "Alice"},          # non-str -> coerced
                {"id": "b"},                          # missing name -> skipped
                {"id": "c", "name": "Cara"},
            ],
            "items": [
                {"id": "i1", "description": "ok", "amount_cents": "5",
                 "payer_id": "1", "participant_ids": ["1", "c"], "split": 7},
            ],
        }
        state = AppState.from_dict(raw)
        ids = sorted(p.id for p in state.people)
        self.assertEqual(ids, ["1", "c"])  # bad record dropped, id stringified
        it = state.items[0]
        self.assertEqual(it.amount_cents, 0)            # non-int coerced
        self.assertEqual(it.split_mode(), MODE_EQUAL)   # non-dict split safe

    def test_from_dict_non_dict_input(self):
        self.assertEqual(AppState.from_dict("garbage").to_dict(),
                         {"people": [], "items": []})

    def test_missing_amount_is_reported_not_silently_zeroed(self):
        # A hand-edited backup saying "amount" must not import as a clean
        # $0.00 item: the import prompt only warns when issues are reported.
        issues = []
        state = AppState.from_dict(
            {"people": [{"id": "a", "name": "A"}],
             "items": [{"id": "i", "description": "Rent", "amount": 120000,
                        "payer_id": "a", "participant_ids": ["a"],
                        "split": {"mode": "equal"}}]},
            on_issue=lambda kind, message: issues.append((kind, message)))

        self.assertEqual(state.items[0].amount_cents, 0)
        self.assertEqual(issues,
                         [("item", "amount_cents is missing; set to 0")])

    def test_console_lines_say_whether_a_record_was_fixed_or_skipped(self):
        # Import points users to the browser console for details, so each
        # line must name its record and tell a record kept with a change
        # ("fixed") from one left out of the bill ("skipped").
        raw = {
            "people": [{"id": "a", "name": "Ann"}, {"id": "b", "name": "Bob"},
                       {"id": "a", "name": "Again"}],
            "items": [
                {"id": "i1", "description": "Rent", "payer_id": "a",
                 "participant_ids": ["a"], "split": {"mode": "equal"}},
                {"id": "i2", "description": "Car",
                 "amount_cents": MAX_CENTS + 1, "payer_id": "a",
                 "participant_ids": ["a"], "split": {"mode": "equal"}},
                {"id": "i3", "description": "Hotel", "amount_cents": 500,
                 "payer_id": "a", "participant_ids": ["a", "b", "a"],
                 "split": {"mode": "uneven",
                           "weights": {"a": "2", "b": 1, "q": 5}}},
                {"id": "i4", "description": "Taxi", "amount_cents": 300,
                 "payer_id": "a", "participant_ids": ["a", "zz"],
                 "split": {"mode": "equal"}},
                {"id": "i5", "description": "Ghost", "amount_cents": 100,
                 "payer_id": "zz", "participant_ids": ["a"],
                 "split": {"mode": "equal"}},
            ],
        }
        err = io.StringIO()

        with contextlib.redirect_stderr(err):
            state = AppState.from_dict(raw)

        self.assertEqual([it.id for it in state.items],
                         ["i1", "i2", "i3", "i4"])
        self.assertEqual(err.getvalue().splitlines(), [
            "bunnysplit: skipped malformed person record 'a' (Again): "
            "duplicate id 'a'",
            "bunnysplit: fixed item record 'i1' (Rent): "
            "amount_cents is missing; set to 0",
            "bunnysplit: fixed item record 'i2' (Car): "
            "amount_cents exceeds MAX_CENTS; set to MAX_CENTS",
            "bunnysplit: skipped malformed participant record in item 'i3' "
            "(Hotel): duplicate id 'a'",
            "bunnysplit: fixed item record 'i3' (Hotel): "
            "dropped invalid weight for 'a'",
            "bunnysplit: fixed item record 'i3' (Hotel): "
            "dropped 1 weight for non-participants",
            "bunnysplit: fixed item record 'i4' (Taxi): "
            "dropped unknown participants",
            "bunnysplit: skipped malformed item record 'i5' (Ghost): "
            "payer 'zz' not in roster",
        ])


class TextFieldTests(unittest.TestCase):
    """Names and descriptions come from untrusted JSON. str() of a deeply
    nested list recurses in C; under MicroPython on wasm that overflows the
    JS stack (a RangeError no Python except clause catches) before any
    length cap is checked, so containers must never reach str()."""

    def _from_dict(self, raw):
        issues = []
        state = AppState.from_dict(
            raw, on_issue=lambda kind, message: issues.append((kind, message)))
        return state, issues

    def test_container_names_skip_the_person(self):
        state, issues = self._from_dict({
            "people": [{"id": "a", "name": [1, 2]},
                       {"id": "b", "name": {"x": 1}},
                       {"id": "c", "name": "C"}],
            "items": []})

        self.assertEqual([p.name for p in state.people], ["C"])
        self.assertEqual(len(issues), 2)

    def test_container_description_is_blanked_and_the_item_kept(self):
        state, issues = self._from_dict({
            "people": [{"id": "a", "name": "A"}],
            "items": [{"id": "i", "description": [["x"]], "amount_cents": 500,
                       "payer_id": "a", "participant_ids": ["a"],
                       "split": {"mode": "equal"}}]})

        self.assertEqual(state.items[0].description, "")
        self.assertEqual(state.items[0].amount_cents, 500)
        self.assertEqual(len(issues), 1)

    def test_scalar_names_and_descriptions_are_still_coerced(self):
        state, issues = self._from_dict({
            "people": [{"id": "a", "name": 42}],
            "items": [{"id": "i", "description": 7, "amount_cents": 500,
                       "payer_id": "a", "participant_ids": ["a"],
                       "split": {"mode": "equal"}}]})

        self.assertEqual(state.people[0].name, "42")
        self.assertEqual(state.items[0].description, "7")
        self.assertEqual(issues, [])


class FromDictBoundaryTests(unittest.TestCase):
    """Persisted state arrives from localStorage and may be hand-edited
    or corrupted. AppState.from_dict / Item.from_dict are the trust
    boundary; these tests pin the hardening behaviors there."""

    def _people(self):
        return [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]

    def test_duplicate_participants_deduped_to_preserve_money(self):
        # Without dedup, both split paths key shares by pid, so a repeat
        # overwrites the prior share and money silently vanishes.
        raw = {
            "people": self._people(),
            "items": [{"id": "i", "description": "x", "amount_cents": 300,
                       "payer_id": "a",
                       "participant_ids": ["a", "a", "b"],
                       "split": {"mode": "equal"}}],
        }
        state = AppState.from_dict(raw)
        self.assertEqual(state.items[0].participant_ids, ["a", "b"])
        shares = split_item(state.items[0])
        self.assertEqual(sum(shares.values()), 300)

    def test_scalar_collections_are_recovered_without_crashing(self):
        issues = []
        state = AppState.from_dict(
            {"people": 7, "items": {"id": "i"}},
            on_issue=lambda kind, message: issues.append((kind, message)),
        )
        self.assertEqual(state.to_dict(), {"people": [], "items": []})
        self.assertEqual([kind for kind, _ in issues], ["people", "items"])

    def test_duplicate_person_and_item_ids_are_skipped(self):
        issues = []
        raw = {
            "people": [
                {"id": "a", "name": "First"},
                {"id": "a", "name": "Duplicate"},
                {"id": "b", "name": "B"},
            ],
            "items": [
                {"id": "i", "description": "first", "amount_cents": 100,
                 "payer_id": "a", "participant_ids": ["a", "b"],
                 "split": {"mode": "equal"}},
                {"id": "i", "description": "duplicate", "amount_cents": 200,
                 "payer_id": "a", "participant_ids": ["a", "b"],
                 "split": {"mode": "equal"}},
            ],
        }
        state = AppState.from_dict(
            raw, on_issue=lambda kind, message: issues.append((kind, message)))
        self.assertEqual([(p.id, p.name) for p in state.people],
                         [("a", "First"), ("b", "B")])
        self.assertEqual([(it.id, it.description) for it in state.items],
                         [("i", "first")])
        self.assertEqual(sum("duplicate id" in message for _, message in issues),
                         2)

    def test_empty_and_oversized_ids_are_skipped(self):
        too_long = "x" * (MAX_ID_LENGTH + 1)
        raw = {
            "people": [
                {"id": "", "name": "Empty"},
                {"id": too_long, "name": "Long"},
                {"id": "a", "name": "A"},
            ],
            "items": [
                {"id": too_long, "description": "bad", "amount_cents": 100,
                 "payer_id": "a", "participant_ids": ["a"],
                 "split": {"mode": "equal"}},
                {"id": "ok", "description": "ok", "amount_cents": 100,
                 "payer_id": "a", "participant_ids": ["a", too_long],
                 "split": {"mode": "equal"}},
            ],
        }
        state = AppState.from_dict(raw)
        self.assertEqual([p.id for p in state.people], ["a"])
        self.assertEqual([it.id for it in state.items], ["ok"])
        self.assertEqual(state.items[0].participant_ids, ["a"])

    def test_composite_and_null_ids_are_skipped(self):
        raw = {
            "people": [
                {"id": None, "name": "Null"},
                {"id": {"nested": "id"}, "name": "Composite"},
                {"id": "a", "name": "A"},
            ],
            "items": [{
                "id": "i", "description": "ok", "amount_cents": 100,
                "payer_id": "a", "participant_ids": ["a", ["bad"]],
                "split": {"mode": "equal"},
            }],
        }
        state = AppState.from_dict(raw)
        self.assertEqual([person.id for person in state.people], ["a"])
        self.assertEqual(state.items[0].participant_ids, ["a"])

    def test_ids_with_a_nul_character_are_skipped_and_reported(self):
        # MicroPython's jsffi cuts a str at its first NUL when it reaches JS,
        # so the DOM would hold "b" for a person whose id is "b\x00": adding
        # an item would then record a participant or payer nobody has, or
        # another person's id, and lose that share.
        issues = []
        raw = {
            "people": [
                {"id": "a", "name": "A"},
                {"id": "b\x00", "name": "B"},
                {"id": "a\x00b", "name": "Also B"},
            ],
            "items": [
                {"id": "i1", "description": "kept", "amount_cents": 100,
                 "payer_id": "a", "participant_ids": ["a", "b\x00"],
                 "split": {"mode": "equal"}},
                {"id": "i2", "description": "bad payer", "amount_cents": 100,
                 "payer_id": "a\x00", "participant_ids": ["a"],
                 "split": {"mode": "equal"}},
                {"id": "i3\x00", "description": "bad id", "amount_cents": 100,
                 "payer_id": "a", "participant_ids": ["a"],
                 "split": {"mode": "equal"}},
            ],
        }

        state = AppState.from_dict(
            raw, on_issue=lambda kind, message: issues.append((kind, message)))

        self.assertEqual([p.id for p in state.people], ["a"])
        self.assertEqual([it.id for it in state.items], ["i1"])
        self.assertEqual(state.items[0].participant_ids, ["a"])
        self.assertEqual(
            sum("NUL" in message for _, message in issues), 5, issues)

    def test_unknown_split_mode_normalized_to_equal(self):
        raw = {
            "people": self._people(),
            "items": [{"id": "i", "description": "x", "amount_cents": 100,
                       "payer_id": "a", "participant_ids": ["a", "b"],
                       "split": {"mode": "weird-future-mode"}}],
        }
        state = AppState.from_dict(raw)
        self.assertEqual(state.items[0].split_mode(), MODE_EQUAL)
        # Calc layer no longer raises on this item.
        self.assertEqual(sum(split_item(state.items[0]).values()), 100)

    def test_negative_amount_clamped_to_zero(self):
        # Equal split would conserve negatives, but the uneven path
        # truncates toward zero and leaks a cent. Reject at the boundary.
        raw = {
            "people": self._people(),
            "items": [{"id": "i", "description": "x", "amount_cents": -500,
                       "payer_id": "a", "participant_ids": ["a", "b"],
                       "split": {"mode": "equal"}}],
        }
        state = AppState.from_dict(raw)
        self.assertEqual(state.items[0].amount_cents, 0)

    def test_orphan_payer_drops_item(self):
        raw = {
            "people": self._people(),
            "items": [{"id": "i", "description": "x", "amount_cents": 300,
                       "payer_id": "ghost",
                       "participant_ids": ["a", "b"],
                       "split": {"mode": "equal"}}],
        }
        state = AppState.from_dict(raw)
        self.assertEqual(state.items, [])

    def test_item_with_only_orphan_participants_dropped(self):
        raw = {
            "people": self._people(),
            "items": [{"id": "i", "description": "x", "amount_cents": 300,
                       "payer_id": "a",
                       "participant_ids": ["ghost1", "ghost2"],
                       "split": {"mode": "equal"}}],
        }
        state = AppState.from_dict(raw)
        self.assertEqual(state.items, [])

    def test_partial_orphans_filtered_from_participants(self):
        # Known participants stay; unknown ones are removed and weights
        # for them are dropped too.
        raw = {
            "people": self._people(),
            "items": [{"id": "i", "description": "x", "amount_cents": 600,
                       "payer_id": "a",
                       "participant_ids": ["a", "ghost", "b"],
                       "split": {"mode": "uneven",
                                 "weights": {"a": 1, "ghost": 5, "b": 1}}}],
        }
        state = AppState.from_dict(raw)
        it = state.items[0]
        self.assertEqual(it.participant_ids, ["a", "b"])
        self.assertEqual(it.split.get("weights"), {"a": 1, "b": 1})
        # Settlement now conserves: a paid 600, owes 300; b owes 300.
        self.assertEqual(settle_up(state), [("b", "a", 300)])

    def test_well_formed_state_survives_untouched(self):
        # Regression guard: the new filters must not chip away at valid
        # state. A clean round-trip must equal the input.
        raw = {
            "people": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
            "items": [{"id": "i", "description": "lunch",
                       "amount_cents": 1234, "payer_id": "a",
                       "participant_ids": ["a", "b"],
                       "split": {"mode": "uneven",
                                 "weights": {"a": 2, "b": 1}}}],
        }
        self.assertEqual(AppState.from_dict(raw).to_dict(), raw)

    def test_oversized_amount_clamped(self):
        raw = {
            "people": self._people(),
            "items": [{"id": "i", "description": "x", "amount_cents": 10 ** 50,
                       "payer_id": "a", "participant_ids": ["a", "b"],
                       "split": {"mode": "equal"}}],
        }
        state = AppState.from_dict(raw)
        self.assertEqual(state.items[0].amount_cents, MAX_CENTS)

    def test_huge_amount_uneven_conserves_after_clamp(self):
        # The IndexError repro: 10**20 split 1:1:1. After clamping, the
        # float split stays exact and conserves to MAX_CENTS.
        raw = {
            "people": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"},
                       {"id": "c", "name": "C"}],
            "items": [{"id": "i", "description": "x", "amount_cents": 10 ** 20,
                       "payer_id": "a", "participant_ids": ["a", "b", "c"],
                       "split": {"mode": "uneven",
                                 "weights": {"a": 1, "b": 1, "c": 1}}}],
        }
        it = AppState.from_dict(raw).items[0]
        self.assertEqual(it.amount_cents, MAX_CENTS)
        self.assertEqual(sum(split_item(it).values()), MAX_CENTS)


class HugeWeightTests(unittest.TestCase):
    def test_huge_weight_does_not_crash_and_conserves(self):
        # 1e308 is finite, so parse_finite accepts it; capping prevents
        # amount * weight from overflowing to inf (OverflowError on int()).
        shares = split_item(item(1000, ["a", "b"],
                                 split=uneven({"a": 1e308, "b": 1})))
        self.assertEqual(sum(shares.values()), 1000)


class SplitSanitizingTests(unittest.TestCase):
    """Item.from_dict keeps only split data that survives a JSON round trip,
    and only finite numbers as weights.

    json.loads turns 1e309 into inf, and dumps() writes that back as a bare
    inf/Infinity that json.loads rejects, so the next load would drop the
    whole bill. split_item() already treats a non-finite weight as 0, so
    dropping one does not change the computed split."""

    def _state(self, split, ids="ab"):
        issues = []
        state = AppState.from_dict(
            {"people": [{"id": pid, "name": pid.upper()} for pid in ids],
             "items": [{"id": "i", "description": "x", "amount_cents": 1000,
                        "payer_id": "a", "participant_ids": list(ids),
                        "split": split}]},
            on_issue=lambda kind, message: issues.append((kind, message)))
        return state, issues

    def assert_strict_json(self, state):
        json.dumps(state.to_dict(), allow_nan=False)

    def test_overflowing_weight_is_dropped_and_split_is_unchanged(self):
        split = json.loads('{"mode": "uneven", "weights": {"a": 1e309, "b": 2}}')
        before = split_item(item(1000, ["a", "b"], split=dict(split)))

        state, issues = self._state(split)

        self.assertEqual(state.items[0].split,
                         {"mode": MODE_UNEVEN, "weights": {"b": 2}})
        self.assertEqual(split_item(state.items[0]), before)
        self.assertEqual(len(issues), 1)
        self.assert_strict_json(state)

    def test_nan_and_negative_infinity_weights_are_dropped(self):
        state, issues = self._state(uneven({"a": float("nan"),
                                            "b": float("-inf")}))

        self.assertEqual(state.items[0].weights(), {})
        self.assertEqual(len(issues), 2)
        self.assert_strict_json(state)

    def test_container_weights_are_dropped(self):
        # A container could hide a non-finite number anywhere inside it.
        state, issues = self._state(uneven({"a": [1e309], "b": {"x": 1}}))

        self.assertEqual(state.items[0].weights(), {})
        self.assertEqual(len(issues), 2)
        self.assert_strict_json(state)

    def test_only_finite_numbers_are_kept_as_weights(self):
        # The app only ever saves float weights. A string weight would reach
        # the UI's weight formatting, and MicroPython's int() of a long digit
        # string is quadratic, so it would freeze every render.
        weights = {"a": 1, "b": "2", "c": 0.5, "d": True, "e": None,
                   "f": "7" * 1000}

        state, issues = self._state(uneven(dict(weights)), ids="abcdef")

        self.assertEqual(state.items[0].weights(), {"a": 1, "c": 0.5})
        self.assertEqual(len(issues), 4)
        self.assert_strict_json(state)

    def test_weights_for_non_participants_are_dropped_with_one_issue(self):
        # split_item() reads only participants' weights, and the app writes
        # no others. Under MicroPython, building one dict is superlinear in
        # its key count: 40,000 extra keys fit a 380 KiB backup and made
        # every later page load take ~12 s. Reporting each key would also
        # flood the console, so the extra keys are one issue per item.
        weights = {"a": 1, "b": 2, "x": 1, "y": None, "z": "7"}
        before = split_item(item(1000, ["a", "b"], split=uneven(dict(weights))))

        state, issues = self._state(uneven(dict(weights)))

        self.assertEqual(state.items[0].weights(), {"a": 1, "b": 2})
        self.assertEqual(split_item(state.items[0]), before)
        self.assertEqual(issues,
                         [("item", "dropped 3 weights for non-participants")])
        self.assert_strict_json(state)

    def test_unknown_split_fields_are_dropped(self):
        for split, expected in (
            ({"mode": "equal", "junk": [float("inf")], "weights": {"a": 1}},
             {"mode": MODE_EQUAL}),
            ({"mode": "uneven", "weights": {"a": 1}, "extra": float("inf")},
             {"mode": MODE_UNEVEN, "weights": {"a": 1}}),
        ):
            with self.subTest(split=split):
                state, issues = self._state(split)

                self.assertEqual(state.items[0].split, expected)
                self.assertEqual(len(issues), 1)
                self.assert_strict_json(state)


if __name__ == "__main__":
    unittest.main()
