"""Split and settle-up math. Pure functions over the model; integer cents.

Every function guarantees money conservation: the sum of allocated cents
always equals the input amount exactly (no lost or invented pennies).
"""

from splitcore.model import MAX_CENTS, MAX_WEIGHT, MODE_EQUAL, MODE_UNEVEN

_MAX_WHOLE_DIGITS = len(str(MAX_CENTS // 100))
MAX_AMOUNT_INPUT_LENGTH = _MAX_WHOLE_DIGITS + 4  # sign, dot, two+ decimals


def parse_finite(x):
    """Parse x to a real, finite float, or return None.

    Single source of truth for the untrusted-input boundary: amounts/weights
    arrive from typed input or hand-edited localStorage. MicroPython lacks
    math.isfinite, so detect NaN via self-inequality and inf via abs().
    OverflowError can come from MicroPython float() on huge magnitudes.
    """
    try:
        f = float(x)
    except (ValueError, TypeError, OverflowError):
        return None
    return f if (f == f and abs(f) != float("inf")) else None


def is_finite_number(x):
    """True only for a real, finite number. Rejects NaN and +/-inf."""
    return parse_finite(x) is not None


def parse_cents(raw):
    """Parse a money string to exact integer cents, or None.

    String-based: no float arithmetic, so values like '0.295' do NOT lose
    a half-cent the way int(round(float('0.295') * 100)) does. Truncates
    fractional digits past 2 (not banker's-round); pads with zeros if the
    user typed fewer than 2. Accepts an optional leading sign.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if len(s) > MAX_AMOUNT_INPUT_LENGTH:
        return None
    neg = False
    if s[0] in "+-":
        neg = s[0] == "-"
        s = s[1:]
    if not s or s == "." or s.count(".") > 1:
        return None
    if "." in s:
        whole, frac = s.split(".", 1)
    else:
        whole, frac = s, ""
    if whole and not whole.isdigit():
        return None
    if frac and not frac.isdigit():
        return None
    if not whole and not frac:
        return None
    # Bound conversion cost before int(). This also avoids CPython's maximum
    # decimal-string conversion exception and equivalent MicroPython pressure.
    if len(whole) > _MAX_WHOLE_DIGITS:
        return None
    cents = int(whole or "0") * 100 + int((frac + "00")[:2])
    return -cents if neg else cents


def _equal_shares(participants, amount):
    """Even split; leftover pennies go one each to the first participants."""
    n = len(participants)
    base = amount // n
    remainder = amount - base * n
    return {
        pid: base + (1 if idx < remainder else 0)
        for idx, pid in enumerate(participants)
    }


def split_item(item):
    """Return {person_id: cents} splitting one item among its participants.

    Equal mode: even division; leftover pennies go one each to the first
    participants in order.

    Uneven mode: proportional to weights; floor each share, then hand the
    remaining pennies to the participants with the largest fractional
    remainder (ties broken by participant order).
    """
    participants = item.participant_ids
    n = len(participants)
    if n == 0:
        return {}

    amount = item.amount_cents
    mode = item.split_mode()

    if mode == MODE_EQUAL:
        return _equal_shares(participants, amount)

    if mode == MODE_UNEVEN:
        weights = item.weights()
        w = []
        for pid in participants:
            v = parse_finite(weights.get(pid, 0))
            if v is None or v <= 0:
                v = 0.0
            elif v > MAX_WEIGHT:
                v = MAX_WEIGHT
            w.append(v)
        total_w = sum(w)
        # `not (> 0)` also rejects 0 and NaN totals; fall back to an equal
        # split so money is still conserved rather than crashing or
        # silently dropping the amount on degenerate/hostile weights.
        if not (total_w > 0):
            return _equal_shares(participants, amount)

        exact = [amount * wi / total_w for wi in w]
        floors = [int(x) for x in exact]
        distributed = sum(floors)
        remainder = amount - distributed

        order = sorted(
            range(n),
            key=lambda i: (-(exact[i] - floors[i]), i),
        )
        shares = {}
        for idx, pid in enumerate(participants):
            shares[pid] = floors[idx]
        for k in range(remainder):
            shares[participants[order[k]]] += 1
        return shares

    raise ValueError("unknown split mode: %r" % mode)


def per_person_totals(state):
    """Return {person_id: cents owed} summed across all items."""
    totals = {p.id: 0 for p in state.people}
    for item in state.items:
        for pid, cents in split_item(item).items():
            totals[pid] = totals.get(pid, 0) + cents
    return totals


def settle_up(state):
    """Return a list of (debtor_id, creditor_id, cents) transfers.

    net = total_paid - total_owed. Greedily match the largest debtor with the
    largest creditor until every net is zero.
    """
    owed = per_person_totals(state)
    paid = {p.id: 0 for p in state.people}
    for item in state.items:
        paid[item.payer_id] = paid.get(item.payer_id, 0) + item.amount_cents

    net = {}
    for p in state.people:
        net[p.id] = paid.get(p.id, 0) - owed.get(p.id, 0)

    transfers = []
    if not net:
        return transfers
    while True:
        creditor = max(net, key=lambda k: net[k])
        debtor = min(net, key=lambda k: net[k])
        if net[creditor] <= 0 or net[debtor] >= 0:
            break
        amount = min(net[creditor], -net[debtor])
        transfers.append((debtor, creditor, amount))
        net[creditor] -= amount
        net[debtor] += amount
    return transfers
