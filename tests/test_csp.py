"""Content-Security-Policy in index.html: it must allow exactly what the app
loads, or the app silently fails to boot."""

import pathlib
import re
import unittest
from html.parser import HTMLParser


ROOT = pathlib.Path(__file__).resolve().parent.parent

# PyScript loads MicroPython from jsDelivr at a version fixed per release
# (see its core.js). Bumping PyScript fails these tests until this table and
# the policy's MicroPython path are updated and the app is rechecked in a
# browser for CSP violations.
PYSCRIPT_MICROPYTHON = {"2024.11.1": "1.24.0"}
MICROPYTHON_BASE = (
    "https://cdn.jsdelivr.net/npm/@micropython/"
    "micropython-webassembly-pyscript@%s/")


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.order = []  # (tag, attrs) in document order; comments excluded
        self.inline_scripts = 0
        self._in_script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.order.append((tag, attrs))
        if tag == "script":
            self._in_script = attrs

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = None

    def handle_data(self, data):
        if self._in_script is not None and "src" not in self._in_script \
                and data.strip():
            self.inline_scripts += 1


def _page():
    page = _Page()
    page.feed((ROOT / "index.html").read_text(encoding="utf-8"))
    return page


def _policy(page):
    metas = [a for t, a in page.order if t == "meta"
             and a.get("http-equiv", "").lower() == "content-security-policy"]
    if len(metas) != 1:
        raise AssertionError("expected one CSP meta tag, found %d" % len(metas))
    directives = {}
    for part in metas[0]["content"].split(";"):
        tokens = part.split()
        if tokens:
            directives[tokens[0].lower()] = tokens[1:]
    return directives


def _sources(policy, directive):
    # Fallbacks used by browsers for the directives this app relies on.
    for name in (directive, "default-src"):
        if name in policy:
            return policy[name]
    return []


def _allows(sources, url):
    if not re.match(r"^[a-z]+:", url):  # relative URL: same origin
        return "'self'" in sources
    url = url.split("?", 1)[0]
    for src in sources:
        if not src.startswith("https://"):
            continue
        # A source ending in "/" matches that path prefix; otherwise it
        # names a whole origin.
        prefix = src if src.endswith("/") else src + "/"
        if url.startswith(prefix):
            return True
    return False


class ContentSecurityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.page = _page()
        self.policy = _policy(self.page)

    def test_policy_comes_before_everything_it_governs(self):
        tags = [t for t, a in self.page.order]
        csp_at = next(i for i, (t, a) in enumerate(self.page.order)
                      if t == "meta" and a.get("http-equiv", "").lower()
                      == "content-security-policy")
        first_load = min(i for i, t in enumerate(tags) if t in ("link", "script"))
        self.assertLess(csp_at, first_load)

    def test_default_is_deny(self):
        self.assertEqual(self.policy.get("default-src"), ["'none'"])
        self.assertEqual(self.policy.get("base-uri"), ["'none'"])
        self.assertEqual(self.policy.get("form-action"), ["'none'"])

    def test_every_resource_index_html_loads_is_allowed(self):
        directive_for = {"stylesheet": "style-src", "manifest": "manifest-src",
                         "icon": "img-src", "apple-touch-icon": "img-src"}
        checked = 0
        for tag, attrs in self.page.order:
            if tag == "link" and attrs.get("rel") in directive_for:
                directive, url = directive_for[attrs["rel"]], attrs["href"]
            elif tag == "script" and "src" in attrs:
                # type="mpy" is fetched by PyScript, not run by the browser.
                directive = ("connect-src" if attrs.get("type") == "mpy"
                             else "script-src")
                url = attrs["src"]
            else:
                continue
            checked += 1
            with self.subTest(url=url):
                self.assertTrue(
                    _allows(_sources(self.policy, directive), url),
                    "%s does not allow %s" % (directive, url))
        self.assertGreaterEqual(checked, 7)

    def test_pyscript_and_micropython_paths_match_the_pinned_release(self):
        core = next(a["src"] for t, a in self.page.order
                    if t == "script" and a.get("src", "").endswith("/core.js"))
        version = re.search(r"/releases/([^/]+)/core\.js$", core).group(1)
        pyscript_base = "https://pyscript.net/releases/%s/" % version
        micropython_base = MICROPYTHON_BASE % PYSCRIPT_MICROPYTHON[version]
        self.assertIn(pyscript_base, self.policy["script-src"])
        self.assertIn(pyscript_base, self.policy["style-src"])
        # micropython.mjs is imported as a module, micropython.wasm fetched.
        self.assertIn(micropython_base, self.policy["script-src"])
        self.assertIn(micropython_base, self.policy["connect-src"])

    def test_runtime_needs_only_wasm_compilation(self):
        script = self.policy["script-src"]
        self.assertIn("'wasm-unsafe-eval'", script)
        for keyword in ("'unsafe-eval'", "'unsafe-inline'"):
            self.assertNotIn(keyword, script)
        self.assertNotIn("'unsafe-inline'", self.policy["style-src"])
        # PyScript fetches main.py, the pyscript.toml files and the modules.
        self.assertIn("'self'", self.policy["connect-src"])
        self.assertEqual(self.policy.get("worker-src"), ["'self'"])  # sw.js

    def test_page_has_no_inline_code_the_policy_blocks(self):
        self.assertEqual(self.page.inline_scripts, 0)
        for tag, attrs in self.page.order:
            with self.subTest(tag=tag):
                self.assertNotEqual(tag, "style")
                self.assertNotIn("style", attrs)
                self.assertFalse([a for a in attrs if a.startswith("on")])

    def test_ui_never_writes_a_style_attribute(self):
        # element.style.setProperty() is CSSOM and allowed; a style attribute
        # set from script is blocked without 'unsafe-inline'.
        source = (ROOT / "ui.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"setAttribute\(\s*['\"]style['\"]")
        self.assertNotIn("innerHTML", source.replace(
            "no innerHTML", "").replace("avoid `innerHTML`", ""))

    def test_meta_omits_directives_browsers_ignore_there(self):
        for directive in ("frame-ancestors", "report-uri", "report-to",
                          "sandbox"):
            self.assertNotIn(directive, self.policy)


if __name__ == "__main__":
    unittest.main()
