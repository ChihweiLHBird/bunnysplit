"""DOM rendering and event handlers — the only browser-aware UI module.

State flow: handler -> mutate AppState -> storage.save -> re-render regions.
Dynamic content is built with createElement + textContent (no innerHTML),
so user-entered names cannot inject markup.

Workspace design notes:
- Chip toggle (.chip on/off) and uneven-mode weight input visibility are
  driven by CSS :has() against the native <input> states — no JS toggling.
- Avatars are deterministic per person id (4-color palette) so colors are
  stable across renders without storing them in the model.
"""

import json

from pyscript import document, window
from pyscript.ffi import create_proxy

from splitcore.calc import (
    MAX_AMOUNT_INPUT_LENGTH,
    parse_cents,
    parse_finite,
    per_person_totals,
    settle_up,
)
from splitcore.model import (
    MAX_CENTS, AppState, MODE_EQUAL, MODE_UNEVEN, Item, Person)

_state: AppState = None  # type: ignore  # bound in start()
_storage = None
_id_counter = 0
# Proxies created during a render. They must be destroyed before the next
# render or each mutation permanently leaks one proxy per row.
_render_proxies = []


# ---------- small DOM helpers ----------

def _qs(sel):
    return document.querySelector(sel)


def _qsa(sel):
    nodes = document.querySelectorAll(sel)
    return [nodes.item(i) for i in range(nodes.length)]


def _clear(node):
    while node.firstChild:
        node.removeChild(node.firstChild)


def _el(tag, text=None, cls=None):
    node = document.createElement(tag)
    if text is not None:
        node.textContent = text
    if cls is not None:
        node.className = cls
    return node


def _on(node, event, handler, track=True):
    proxy = create_proxy(handler)
    if track:
        _render_proxies.append(proxy)
    node.addEventListener(event, proxy)


def _money(cents):
    # Place the sign before the currency, not between '$' and the digits.
    sign = "-" if cents < 0 else ""
    return "%s$%.2f" % (sign, abs(cents) / 100.0)


def _next_id(prefix):
    global _id_counter
    existing = {obj.id for obj in list(_state.people) + list(_state.items)}
    while True:
        _id_counter += 1
        candidate = "%s%d" % (prefix, _id_counter)
        if candidate not in existing:
            return candidate


def _seed_counter():
    """Reset generation; _next_id checks loaded logical keys directly."""
    global _id_counter
    _id_counter = 0


# ---------- workspace helpers ----------

def _people_by_id():
    return {p.id: p for p in _state.people}


def _name(by_id, pid):
    person = by_id.get(pid)
    return person.name if person else "?"


def _first_name(name):
    parts = (name or "").strip().split()
    return parts[0] if parts else (name or "?")


def _initials(name):
    parts = (name or "").strip().split()
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


def _av_class(pid):
    # Deterministic 1..4 from id so colors are stable across renders.
    h = 0
    for ch in (pid or ""):
        h = (h * 31 + ord(ch)) & 0xFFFF
    return "av-%d" % (h % 4 + 1)


def _av(pid, name, small=True):
    cls = "av av-sm " + _av_class(pid) if small else "av " + _av_class(pid)
    return _el("span", _initials(name), cls)


def _paid_by_person():
    """{person_id: cents this person has paid out across items}."""
    paid = {p.id: 0 for p in _state.people}
    for it in _state.items:
        if it.payer_id in paid:
            paid[it.payer_id] += it.amount_cents
    return paid


def _fmt_weight(w):
    try:
        if w == int(w):
            return str(int(w))
        return ("%.2f" % float(w)).rstrip("0").rstrip(".")
    except (ValueError, TypeError):
        return "0"


def _weights_summary(item):
    ws = item.weights()
    parts = [_fmt_weight(ws.get(pid, 0)) for pid in item.participant_ids]
    return "·".join(parts)


# ---------- rendering ----------

def _safe(name, fn):
    """Run a renderer; log + swallow exceptions so one bad sub-render
    can't leave the page half-built with dead handlers."""
    try:
        fn()
    except Exception as e:
        window.console.warn("bunnysplit: " + name + " render failed: " + str(e))


def render_all():
    # Snapshot the OLD proxies. We destroy them AFTER the new render so
    # we never invalidate a handler whose Python frame is still on the
    # stack (this function is itself called from those handlers).
    old_proxies = list(_render_proxies)
    _render_proxies.clear()

    by_id = _people_by_id()
    paid = _paid_by_person()
    try:
        totals = per_person_totals(_state)
    except Exception as e:
        window.console.warn("bunnysplit: totals failed: " + str(e))
        totals = {}
    try:
        transfers = settle_up(_state)
    except Exception as e:
        window.console.warn("bunnysplit: settle-up failed: " + str(e))
        transfers = []

    _safe("people", lambda: _render_people(paid))
    _safe("item form", _render_item_form)
    _safe("items", lambda: _render_items(by_id))
    _safe("results", lambda: _render_results(by_id, totals, transfers))
    _safe("kpis", lambda: _render_kpis(paid, transfers))
    _safe("supporting", lambda: _render_supporting(transfers))

    # Old proxies' DOM nodes have just been replaced by _clear() inside
    # each renderer; destroying them now is safe and frees the JS-side
    # proxy table.
    for proxy in old_proxies:
        try:
            proxy.destroy()
        except Exception:
            pass


def _render_people(paid):
    box = _qs("#people-list")
    _clear(box)
    if not _state.people:
        li = _el("li", "No one yet — add a name below.", "muted")
        box.appendChild(li)
    for person in _state.people:
        li = _el("li")
        li.appendChild(_av(person.id, person.name, True))
        li.appendChild(_el("span", person.name, "nm"))
        li.appendChild(_el("span", _money(paid.get(person.id, 0)), "paid num"))
        rm = _el("button", "×", "rm")
        rm.title = "Remove"
        rm.setAttribute("aria-label", "Remove " + person.name)
        _on(rm, "click", _make_remove_person(person.id))
        li.appendChild(rm)
        box.appendChild(li)
    _qs("#people-error").textContent = ""


def _render_item_form():
    payer = _qs("#payer-select")
    _clear(payer)
    if not _state.people:
        placeholder = _el("option", "Add people first")
        placeholder.disabled = True
        payer.appendChild(placeholder)
    for person in _state.people:
        opt = _el("option", person.name)
        opt.value = person.id
        payer.appendChild(opt)

    parts = _qs("#participants")
    _clear(parts)
    if not _state.people:
        parts.appendChild(_el("span", "Add people first.", "empty"))
        return
    for person in _state.people:
        chip = _el("label", None, "chip")
        cb = _el("input")
        cb.type = "checkbox"
        cb.value = person.id
        cb.className = "p-check"
        cb.checked = True
        chip.appendChild(cb)
        chip.appendChild(_av(person.id, person.name, True))
        chip.appendChild(_el("span", _first_name(person.name)))
        wt = _el("input")
        wt.type = "number"
        wt.value = "1"
        wt.min = "0"
        wt.step = "any"
        wt.className = "p-weight"
        wt.setAttribute("data-pid", person.id)
        wt.setAttribute("aria-label", "Weight for " + person.name)
        chip.appendChild(wt)
        parts.appendChild(chip)


def _render_items(by_id):
    box = _qs("#items-list")
    _clear(box)
    if not _state.items:
        li = _el("li", "No items yet — add an expense below.", "muted")
        box.appendChild(li)
        return
    for it in _state.items:
        li = _el("li", None, "table-row")

        li.appendChild(_el("span", "$", "item-icon"))

        body = _el("div", None, "body")
        body.appendChild(_el("div", it.description, "item-title"))
        names = [_name(by_id, pid) for pid in it.participant_ids]
        body.appendChild(_el("div", "Among " + ", ".join(names),
                             "item-among"))
        li.appendChild(body)

        pb = _el("div", None, "paid-by")
        payer = by_id.get(it.payer_id)
        if payer is not None:
            pb.appendChild(_av(payer.id, payer.name, True))
            pb.appendChild(_el("span", _first_name(payer.name), "nm"))
        else:
            pb.appendChild(_el("span", "?", "nm"))
        li.appendChild(pb)

        bwrap = _el("div", None, "badge-cell")
        if it.split_mode() == MODE_UNEVEN:
            bwrap.appendChild(
                _el("span", "Uneven · " + _weights_summary(it),
                    "badge uneven")
            )
        else:
            bwrap.appendChild(_el("span", "Equally", "badge"))
        li.appendChild(bwrap)

        li.appendChild(_el("span", _money(it.amount_cents), "amt num"))

        rm = _el("button", "✕", "btn-x")
        rm.title = "Remove"
        # Include the description so screen-reader users can tell delete
        # buttons apart in a long list. Fall back to a generic label if
        # the description is empty.
        rm.setAttribute(
            "aria-label", "Remove " + (it.description or "item"))
        _on(rm, "click", _make_remove_item(it.id))
        li.appendChild(rm)

        box.appendChild(li)


def _render_results(by_id, totals, transfers):
    box = _qs("#results")
    _clear(box)

    # Total block
    total_cents = sum(i.amount_cents for i in _state.items)
    block = _el("div", None, "total-block")
    block.appendChild(_el("div", "Total billed", "lbl"))
    big = _el("div", None, "big")
    big.appendChild(_el("span", "$", "currency"))
    big.appendChild(_el("span", "%.2f" % (total_cents / 100.0), "num"))
    block.appendChild(big)
    n_items = len(_state.items)
    n_people = len(_state.people)
    meta = "across %d %s, %d %s" % (
        n_items, "item" if n_items == 1 else "items",
        n_people, "person" if n_people == 1 else "people",
    )
    block.appendChild(_el("div", meta, "meta"))
    box.appendChild(block)

    # Each share
    shares_sec = _el("div")
    shares_sec.appendChild(_el("h3", "Each share"))
    if not _state.people:
        shares_sec.appendChild(_el("p", "Add people to see shares.", "muted"))
    else:
        max_share = 0
        for p in _state.people:
            v = totals.get(p.id, 0)
            if v > max_share:
                max_share = v
        ul = _el("ul", None, "rail-shares")
        people_sorted = sorted(
            _state.people, key=lambda p: -totals.get(p.id, 0))
        for idx, person in enumerate(people_sorted):
            share = totals.get(person.id, 0)
            li = _el("li")
            li.appendChild(_av(person.id, person.name, True))
            wrap = _el("div")
            row = _el("div", None, "row")
            row.appendChild(_el("span", _first_name(person.name), "nm"))
            row.appendChild(_el("span", _money(share), "a num"))
            wrap.appendChild(row)
            bar = _el("div", None, "bar")
            fill = _el("span", None, "bar-fill")
            scale = (share / max_share) if max_share > 0 else 0.0
            fill.style.setProperty("--scale", "%.3f" % scale)
            fill.style.setProperty("--d", "%dms" % (100 + idx * 40))
            bar.appendChild(fill)
            wrap.appendChild(bar)
            li.appendChild(wrap)
            ul.appendChild(li)
        shares_sec.appendChild(ul)
    box.appendChild(shares_sec)

    # Settle up
    settle_sec = _el("div")
    n_t = len(transfers)
    settle_sec.appendChild(
        _el("h3", "Settle up · %d %s"
            % (n_t, "transfer" if n_t == 1 else "transfers"))
    )
    if not transfers:
        settle_sec.appendChild(
            _el("p", "All settled — no transfers needed.", "muted")
        )
    for debtor_id, creditor_id, amount in transfers:
        d = by_id.get(debtor_id)
        c = by_id.get(creditor_id)
        card = _el("div", None, "transfer-card")
        ft = _el("div", None, "from-to")
        if d is not None:
            ft.appendChild(_av(d.id, d.name, True))
        ft.appendChild(_el("span", _first_name(d.name) if d else "?", "nm"))
        ft.appendChild(_el("span", "→", "arr"))
        if c is not None:
            ft.appendChild(_av(c.id, c.name, True))
        ft.appendChild(_el("span", _first_name(c.name) if c else "?", "nm"))
        card.appendChild(ft)
        card.appendChild(_el("span", _money(amount), "amt num"))
        settle_sec.appendChild(card)
    box.appendChild(settle_sec)


def _render_kpis(paid, transfers):
    total_cents = sum(i.amount_cents for i in _state.items)
    n_items = len(_state.items)
    n_people = len(_state.people)

    _qs("#kpi-total").textContent = _money(total_cents)
    _qs("#kpi-total-sub").textContent = (
        "%d %s" % (n_items, "item" if n_items == 1 else "items"))

    if n_people > 0:
        avg_cents = total_cents // n_people
        _qs("#kpi-avg").textContent = _money(avg_cents)
        _qs("#kpi-avg-sub").textContent = (
            "over %d %s" % (n_people, "head" if n_people == 1 else "heads"))
    else:
        _qs("#kpi-avg").textContent = _money(0)
        _qs("#kpi-avg-sub").textContent = "over 0 heads"

    # Top contributor — iterate _state.people (list order, stable across
    # renders) so ties resolve deterministically on every runtime,
    # including MicroPython where dict iteration order isn't guaranteed.
    top_id = None
    top_amount = 0
    for person in _state.people:
        amt = paid.get(person.id, 0)
        if amt > top_amount:
            top_amount = amt
            top_id = person.id
    if top_id is not None and top_amount > 0:
        top_person = _state.person_by_id(top_id)
        _qs("#kpi-top").textContent = (
            _first_name(top_person.name) if top_person else "—")
        _qs("#kpi-top-sub").textContent = "paid " + _money(top_amount)
    else:
        _qs("#kpi-top").textContent = "—"
        _qs("#kpi-top-sub").textContent = "no payments yet"

    n_t = len(transfers)
    _qs("#kpi-transfers").textContent = str(n_t)
    _qs("#kpi-transfers-sub").textContent = (
        "all settled" if n_t == 0
        else ("to fully settle" if n_t > 1 else "to settle the bill"))


def _render_supporting(transfers):
    n_items = len(_state.items)
    n_people = len(_state.people)
    n_t = len(transfers)
    if n_items == 0 and n_people == 0:
        msg = "Add people, then add items. Splits and settle-up update live."
    elif n_items == 0:
        msg = ("%d %s, no items yet." %
               (n_people, "person" if n_people == 1 else "people"))
    elif n_t == 0:
        msg = ("%d %s billed across %d %s. All settled." %
               (n_items, "item" if n_items == 1 else "items",
                n_people, "person" if n_people == 1 else "people"))
    else:
        msg = ("%d %s billed across %d %s. %d %s will settle the bill." %
               (n_items, "item" if n_items == 1 else "items",
                n_people, "person" if n_people == 1 else "people",
                n_t, "transfer" if n_t == 1 else "transfers"))
    _qs("#page-supporting").textContent = msg


def _set_save_status(ok, detail=""):
    """Reflect save-attempt outcome in the topbar's Saved pill so users
    can't believe their data is durable while saves are silently
    failing. Updates text, color (via .err class), and title tooltip."""
    pill = _qs(".saved-pill")
    if pill is None:
        return
    label = pill.querySelector(".saved-label")
    recovery = ""
    if ok and _storage is not None:
        try:
            recovery = _storage.recovery_warning()
        except (AttributeError, TypeError):
            pass
    if ok and not recovery:
        try:
            pill.classList.remove("err")
        except Exception:
            pass
        if label is not None:
            label.textContent = "Saved · localStorage"
        pill.title = "State is saved locally in your browser"
    else:
        try:
            pill.classList.add("err")
        except Exception:
            pass
        if recovery:
            if label is not None:
                label.textContent = "Recovered corrupt data"
            msg = recovery
        else:
            if label is not None:
                label.textContent = "Save failed"
            msg = "Could not save to localStorage"
            if detail:
                msg += ": " + detail
        pill.title = msg


def _persist_and_render():
    """Save then re-render. A storage failure (quota, private mode) must
    not desync memory from the DOM or surface a traceback. We log,
    flip the pill to show the failure, and still render so the user
    can keep working in-memory."""
    try:
        _storage.save(_state)
        _set_save_status(True)
    except Exception as e:
        window.console.warn("bunnysplit: could not save: " + str(e))
        _set_save_status(False, str(e))
    render_all()


# ---------- handlers ----------

def on_add_person(event):
    field = _qs("#person-name")
    name = field.value.strip()
    err = _qs("#people-error")
    if not name:
        err.textContent = "Enter a name."
        return
    if any(p.name == name for p in _state.people):
        err.textContent = "That name already exists."
        return
    _state.people.append(Person(_next_id("p"), name))
    field.value = ""
    _persist_and_render()


def _make_remove_person(pid):
    def handler(event):
        refs = _state.items_referencing(pid)
        if refs:
            descs = ", ".join(i.description for i in refs)
            _qs("#people-error").textContent = (
                "Can't remove — used by item(s): " + descs)
            return
        _state.people = [p for p in _state.people if p.id != pid]
        _persist_and_render()
    return handler


def _select_share_field(event):
    """Select a share field's text on focus so the value can be replaced
    with one keystroke. We defer via setTimeout(0) so mouseup (which
    fires after focus and would otherwise clear the selection) runs
    first. Bound only to focusin — a click listener would re-select on
    every subsequent click in the focused field and block caret
    positioning."""
    el = event.target
    if not el.classList.contains("p-weight"):
        return
    proxy_box = []

    def _do_select(*_args):
        try:
            el.select()
        finally:
            if proxy_box:
                try:
                    proxy_box[0].destroy()
                except Exception:
                    pass

    p = create_proxy(_do_select)
    proxy_box.append(p)
    window.setTimeout(p, 0)


def on_add_item(event):
    err = _qs("#item-error")
    err.textContent = ""

    desc = _qs("#item-desc").value.strip()
    if not desc:
        err.textContent = "Enter a description."
        return

    amount_cents = parse_cents(_qs("#item-amount").value)
    if amount_cents is None:
        err.textContent = "Amount must be a number."
        return
    if amount_cents > MAX_CENTS:
        err.textContent = "Amount is unreasonably large."
        return
    if amount_cents <= 0:
        err.textContent = "Amount must be greater than zero."
        return

    payer_id = _qs("#payer-select").value
    if not payer_id or _state.person_by_id(payer_id) is None:
        err.textContent = "Select who paid."
        return

    checked = [c for c in _qsa(".p-check") if c.checked]
    participant_ids = [c.value for c in checked]
    if not participant_ids:
        err.textContent = "Pick at least one participant."
        return

    uneven = _qs("#mode-uneven").checked
    if uneven:
        weights = {}
        for w in _qsa(".p-weight"):
            pid = w.getAttribute("data-pid")
            if pid in participant_ids:
                val = parse_finite(w.value)
                if val is None:
                    err.textContent = "Weights must be numbers."
                    return
                if val < 0:
                    err.textContent = "Weights cannot be negative."
                    return
                weights[pid] = val
        if sum(weights.values()) <= 0:
            err.textContent = "Weights cannot all be zero."
            return
        split = {"mode": MODE_UNEVEN, "weights": weights}
    else:
        split = {"mode": MODE_EQUAL}

    _state.items.append(Item(
        _next_id("i"), desc, amount_cents, payer_id, participant_ids, split))
    _qs("#item-desc").value = ""
    _qs("#item-amount").value = ""
    _persist_and_render()


def _make_remove_item(iid):
    def handler(event):
        _state.items = [i for i in _state.items if i.id != iid]
        _persist_and_render()
    return handler


# ---------- import / export ----------

def on_export_state(event):
    data = json.dumps(_state.to_dict(), indent=2)
    href = "data:application/json;charset=utf-8," + window.encodeURIComponent(data)
    link = _el("a")
    link.href = href
    link.download = "bunnysplit-export.json"
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)


def on_import_click(event):
    _qs("#import-file").click()


def _apply_imported_json(text):
    err = _qs("#data-error")
    try:
        raw = json.loads(text)
    except Exception:
        err.textContent = "That file isn't valid JSON."
        return
    issues = []
    new_state = AppState.from_dict(
        raw, on_issue=lambda kind, message: issues.append(kind + ": " + message))
    _state.people = new_state.people
    _state.items = new_state.items
    _seed_counter()
    if issues:
        err.textContent = (
            "Imported with %d issue(s) — some records were skipped or "
            "adjusted; see console for details." % len(issues))
    else:
        err.textContent = "Imported successfully."
    _persist_and_render()


def on_import_file_change(event):
    err = _qs("#data-error")
    err.textContent = ""
    field = event.target
    files = field.files
    if not files or files.length == 0:
        return
    file = files.item(0)

    def _ok(text):
        _apply_imported_json(text)

    def _fail(e):
        window.console.warn("bunnysplit: import read failed: " + str(e))
        err.textContent = "Could not read the selected file."

    try:
        file.text().then(create_proxy(_ok), create_proxy(_fail))
    except Exception as e:
        window.console.warn("bunnysplit: import failed: " + str(e))
        err.textContent = "Could not read the selected file."
    finally:
        # Reset so choosing the same file again still fires "change".
        field.value = ""


# ---------- entry ----------

def _register_service_worker():
    """Install the PWA cache layer. Browsers gate Service Worker support
    on HTTPS or localhost, so registration silently no-ops on file:// or
    insecure origins — that's expected, not an error."""
    try:
        sw = window.navigator.serviceWorker
    except Exception:
        return
    if not sw:
        return
    try:
        promise = sw.register("./sw.js")
    except Exception as e:
        window.console.warn("bunnysplit: sw register threw: " + str(e))
        return

    def _ok(_reg):
        window.console.log("bunnysplit: service worker registered")

    def _err(e):
        window.console.warn("bunnysplit: service worker failed: " + str(e))

    try:
        promise.then(create_proxy(_ok), create_proxy(_err))
    except Exception:
        # .then() shape varies across PyScript runtimes; logging is
        # best-effort. The registration itself has already kicked off.
        pass


def start(state, storage_module):
    global _state, _storage
    _state = state
    _storage = storage_module
    _seed_counter()
    _qs("#item-amount").maxLength = MAX_AMOUNT_INPUT_LENGTH
    _on(_qs("#add-person"), "click", on_add_person, track=False)
    _on(_qs("#add-item"), "click", on_add_item, track=False)
    _on(_qs("#export-state"), "click", on_export_state, track=False)
    _on(_qs("#import-state"), "click", on_import_click, track=False)
    _on(_qs("#import-file"), "change", on_import_file_change, track=False)
    parts = _qs("#participants")
    _on(parts, "focusin", _select_share_field, track=False)
    render_all()
    # Startup load may have hit blocked/private-mode storage; the pill is
    # hardcoded "Saved" in HTML, so correct it before the user is misled.
    if not _storage.writable():
        _set_save_status(False, "localStorage unavailable")
    else:
        _set_save_status(True)
    boot = _qs("#boot-msg")
    if boot:
        boot.remove()
    _register_service_worker()
