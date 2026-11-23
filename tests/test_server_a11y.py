"""Issue #13: the phone layout and accessibility pass.

What a test here can see is the page's source and what the server hands out; the behaviour in a browser (the keyboard on the tabs, Back and reload, the dialog's
focus, a selection that survives a refresh, 360 px without a sideways scroll, axe) was checked in a real Chrome and is described in the pull request.
The numbers that must not slip back are tested exactly: the colour pairs of both schemes are read out of the page's own CSS and measured here."""
from __future__ import annotations

import os
import re
import struct
import unittest
from html.parser import HTMLParser
from unittest import mock

from support import ServerCase, WEB_DIR, legacy_config

HTML = (WEB_DIR / "static" / "index.html").read_text()
JS = (WEB_DIR / "static" / "app.js").read_text()


# ------------------------------------------------------------------ the page's markup

class Markup(HTMLParser):
    """Every element with its attributes, whether it sits inside a <label>, and the ids that a <label for> names."""

    def __init__(self):
        super().__init__()
        self.els, self.label_for, self._labels = [], set(), 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self._labels += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        self.els.append((tag, a, self._labels > 0))

    def handle_endtag(self, tag):
        if tag == "label":
            self._labels -= 1


def parsed():
    m = Markup()
    m.feed(HTML)
    return m


def luminance(hex_color):
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (f(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    x, y = luminance(a), luminance(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def tokens(block):
    return dict(re.findall(r"--([\w-]+):(#[0-9a-fA-F]{3,6})\b", block))


def schemes():
    light = tokens(re.search(r":root\{color-scheme:light dark;(.*?)\}", HTML, re.S).group(1))
    dark_auto = tokens(re.search(r":root:not\(\[data-theme=light\]\)\{(.*?)\}", HTML, re.S).group(1))
    dark_set = tokens(re.search(r":root\[data-theme=dark\]\{(.*?)\}", HTML, re.S).group(1))
    return light, dark_auto, dark_set


class Src(unittest.TestCase):
    """assertIn and assertRegex on the page's source, saying what was missing instead of printing the whole page."""

    def _short(self, kind, needle, text, ok, msg):
        if len(text) > 400:
            if not ok:
                self.fail(msg or f"{kind}: {needle!r}")
        return len(text) <= 400

    def assertIn(self, member, container, msg=None):
        if self._short("missing", member, container, member in container, msg):
            super().assertIn(member, container, msg)

    def assertNotIn(self, member, container, msg=None):
        if self._short("present", member, container, member not in container, msg):
            super().assertNotIn(member, container, msg)

    def assertRegex(self, text, regex, msg=None):
        if self._short("no match", regex, text, re.search(regex, text), msg):
            super().assertRegex(text, regex, msg)

    def assertNotRegex(self, text, regex, msg=None):
        if self._short("matches", regex, text, not re.search(regex, text), msg):
            super().assertNotRegex(text, regex, msg)


class Colours(Src):
    TEXT = (("fg", "card"), ("fg", "bg"), ("muted", "card"), ("muted", "bg"), ("muted", "mut-t"), ("muted", "sel"), ("fg", "sel"), ("link", "card"), ("link", "bg"), ("link", "sel"),
            ("on-acc", "acc"), ("on-bad", "bad-solid"), ("ok", "card"), ("warn", "card"), ("bad", "card"), ("ok", "ok-t"), ("warn", "warn-t"), ("bad", "bad-t"), ("acc", "acc-t"),
            ("mark-fg", "mark"), ("bg", "fg"), ("bg", "bad"))
    UI = (("ctl", "card"), ("ctl", "bg"), ("focus", "card"), ("focus", "bg"), ("acc", "bar"), ("acc", "card"))

    def test_both_schemes_define_the_same_tokens_and_the_two_ways_into_dark_agree(self):
        light, dark_auto, dark_set = schemes()
        self.assertEqual(dark_auto, dark_set)                                              # the system's dark and the button's dark are one palette
        for name in set(light) - {"mark", "mark-fg", "line"} | set():
            self.assertIn(name, dark_set, f"--{name} has no dark value")
        for name in ("mark", "mark-fg"):
            self.assertIn(name, light)                                                     # the search highlight keeps one pair in both (it has its own text colour)

    def test_every_text_pair_is_at_least_4_5_to_1_in_light_and_in_dark(self):
        light, dark, _ = schemes()
        for scheme, palette in (("light", light), ("dark", {**light, **dark})):
            for fg, bg in self.TEXT:
                with self.subTest(scheme=scheme, pair=f"{fg} on {bg}"):
                    self.assertGreaterEqual(ratio(palette[fg], palette[bg]), 4.5)

    def test_the_edges_of_controls_the_focus_ring_and_the_progress_fill_are_at_least_3_to_1(self):
        light, dark, _ = schemes()
        for scheme, palette in (("light", light), ("dark", {**light, **dark})):
            for fg, bg in self.UI:
                with self.subTest(scheme=scheme, pair=f"{fg} on {bg}"):
                    self.assertGreaterEqual(ratio(palette[fg], palette[bg]), 3.0)

    def test_the_dark_primary_button_is_no_longer_white_on_light_blue(self):
        light, dark, _ = schemes()
        self.assertLess(ratio("#ffffff", "#60a5fa"), 2.6)                                   # what it was: white text on the dark scheme's blue (2.54:1)
        self.assertGreaterEqual(ratio(dark["on-acc"], dark["acc"]), 7.0)                    # dark text on it now
        self.assertRegex(HTML, r"button\.pri\{background:var\(--acc\);border-color:var\(--acc\);color:var\(--on-acc\)\}")
        self.assertRegex(HTML, r"button\.dng\{background:var\(--bad-solid\);border-color:var\(--bad-solid\);color:var\(--on-bad\)\}")
        self.assertNotRegex(HTML, r"button\.pri\{[^}]*color:#fff")                          # no colour written past the tokens

    def test_a_disabled_button_is_still_readable(self):
        self.assertRegex(HTML, r"button\.btn:disabled,button\.pri:disabled,button\.dng:disabled\{background:var\(--mut-t\);border-color:var\(--line\);color:var\(--muted\)\}")
        self.assertNotRegex(HTML, r"button:disabled\{[^}]*opacity")                         # half-transparent white on blue was 2.7:1

    def test_status_is_not_told_by_colour_alone(self):
        for cls in ("t-failed", "t-error", "t-built", "t-copied", "t-done", "t-deferred", "t-waiting", "t-cancelled", "t-skip", "t-build", "t-copy"):
            with self.subTest(cls):
                self.assertRegex(HTML, rf"[^{{}}]*\.{cls}::before[^{{}}]*\{{content:")        # each status tag starts with a mark of its own: a tick, a cross, a dash...
        self.assertIn("\\2713", HTML)
        self.assertIn("\\2715", HTML)

    def test_the_theme_follows_the_system_and_a_choice_overrides_it(self):
        self.assertIn('<meta name="color-scheme" content="light dark">', HTML)
        self.assertIn("@media (prefers-color-scheme:dark){:root:not([data-theme=light])", HTML)
        self.assertIn(":root[data-theme=dark]{color-scheme:dark", HTML)
        self.assertIn(":root[data-theme=light]{color-scheme:light}", HTML)
        self.assertRegex(HTML, r'<meta name="theme-color" content="#ffffff" media="\(prefers-color-scheme: light\)">')
        self.assertRegex(HTML, r'<meta name="theme-color" content="#171c23" media="\(prefers-color-scheme: dark\)">')
        self.assertIn('id="bTheme"', HTML)
        theme = JS[JS.index("const THEMES"):JS.index("let tab=")]
        self.assertIn('localStorage.getItem(THEME_KEY)', theme)
        self.assertIn('["auto","light","dark"]', theme)                                     # three states: the system's, light, dark
        self.assertIn("delete r.dataset.theme", theme)                                      # "follow the system" removes the override
        self.assertIn("catch(e){return\"auto\"}", theme)                                    # storage that throws does not break the page

    def test_the_focus_ring_is_visible_and_motion_can_be_turned_off(self):
        self.assertRegex(HTML, r":focus-visible\{outline:3px solid var\(--focus\)")
        removers = [sel.strip() for sel in re.findall(r"([^{}]*)\{[^}]*outline:\s*none", HTML)]
        self.assertEqual(sorted(set(removers)), [":focus", "main:focus,section:focus"])     # only the focus a script gives (the skip link, a panel); a keyboard focus always shows
        self.assertIn("prefers-reduced-motion:reduce", HTML)


class Structure(Src):
    def test_the_five_tabs_are_an_aria_tablist_with_labelled_panels(self):
        m = parsed()
        ids = {a["id"]: a for t, a, _ in m.els if "id" in a}
        tabs = [a for t, a, _ in m.els if a.get("role") == "tab"]
        panels = [a for t, a, _ in m.els if a.get("role") == "tabpanel"]
        self.assertEqual([a["data-tab"] for a in tabs], ["dash", "lib", "hist", "set", "logs"])
        self.assertEqual(sum(1 for t, a, _ in m.els if a.get("role") == "tablist"), 1)
        self.assertEqual([a["aria-selected"] for a in tabs], ["true", "false", "false", "false", "false"])
        self.assertEqual([a["tabindex"] for a in tabs], ["0", "-1", "-1", "-1", "-1"])      # roving tabindex: one stop for the list, arrows inside it
        for a in tabs:
            panel = ids[a["aria-controls"]]
            self.assertEqual(panel["role"], "tabpanel")
            self.assertEqual(panel["aria-labelledby"], a["id"])
        self.assertEqual(len(panels), 5)

    def test_the_tabs_work_from_the_keyboard_and_the_address(self):
        keys = JS[JS.index('$("[role=tablist]").addEventListener("keydown"'):JS.index("function startRoute")]
        for k in ("ArrowRight", "ArrowLeft", "Home", "End"):
            self.assertIn(f'"{k}"', keys)
        self.assertIn("x.tabIndex=j===n?0:-1", keys)
        self.assertNotIn("click()", keys)                                                   # arrows move focus; Enter or Space (the button's own click) opens the tab
        for h in ('dash:"dashboard"', 'lib:"library"', 'hist:"history"', 'set:"settings"', 'logs:"logs"'):
            self.assertIn(h, JS)
        show = JS[JS.index("function showTab"):JS.index("async function route")]
        self.assertIn('setAttribute("aria-selected"', show)
        self.assertIn("document.title=", show)
        self.assertIn("focus({preventScroll:true})", show)                                  # a link to another tab does not leave focus on a hidden element
        route = JS[JS.index("async function route"):JS.index("async function navigate")]
        self.assertIn("lastRoute", route)                                                   # popstate and hashchange both arrive: the second is ignored
        self.assertIn("setHash(hashOf(tab))", route)                                        # "keep editing" puts the address back
        self.assertIn('window.addEventListener("popstate"', JS)
        self.assertIn('window.addEventListener("hashchange"', JS)
        nav = JS[JS.index("async function navigate"):JS.index("const goTab")]
        self.assertIn("setHash(h,true)", nav)                                               # a tab or link is a history entry, so Back works
        self.assertIn("if(t===tab&&!sub&&!qs)return true", nav)                             # the same tab again is not another entry
        self.assertNotIn("nav button", JS)                                                  # nothing finds a tab by clicking the nav any more
        self.assertIn("startRoute()", JS)                                                   # the first view comes from the address: a reload stays on the tab

    def test_a_game_can_be_linked_to_and_the_link_opens_its_row(self):
        route = JS[JS.index("async function route"):JS.index("async function navigate")]
        self.assertIn("wantRow=r.sub", route)
        focus = JS[JS.index("function focusRow"):JS.index("function applyLibFilter")]
        self.assertIn("details.why", focus)
        self.assertIn("scrollIntoView", focus)
        self.assertIn("is not in the Library", focus)                                       # an unknown game is said, not silently ignored
        self.assertIn("focusRow()", JS[JS.index("function renderLib"):JS.index("// what the user had open")])   # also when the list is loaded after the address
        self.assertIn('href="#library/${esc(encodeURIComponent(p.tid))}"', JS)               # History links to the row

    def test_every_control_has_a_name(self):
        m = parsed()
        named = []
        for tag, a, in_label in m.els:
            if tag in ("input", "select", "textarea") and a.get("type") != "hidden":
                ok = in_label or a.get("aria-label") or a.get("aria-labelledby") or (a.get("id") in m.label_for)
                named.append((a.get("id"), bool(ok)))
                self.assertTrue(ok, f"unlabelled <{tag} id={a.get('id')}>")                   # a placeholder or a title is not a name
        self.assertGreaterEqual(len(named), 14)
        for id_ in ("libFilter", "libAct", "pkgFind", "pkgStatus", "pkgSort", "logFind", "logSearch", "pauseUntil"):
            self.assertIn((id_, True), named)
        self.assertIn('aria-label="Time of day, as HH:MM"', JS)                              # the schedule's inputs and the import file are built by the script
        self.assertIn('aria-label="Settings file to import"', JS)
        for b in re.findall(r"<button\b[^>]*></button>", HTML):
            self.fail("a button with no content: " + b)
        for tag, a, _ in m.els:                                                              # a button with text is named by it; the icon-only ones say what they are
            if tag == "button" and a.get("id") in ("bTheme", "logPrev", "logNext"):
                self.assertTrue(a.get("aria-label"), a.get("id"))

    def test_the_page_has_a_language_landmarks_a_skip_link_and_may_be_zoomed(self):
        self.assertIn('<html lang="en">', HTML)
        self.assertRegex(HTML, r'<body>\s*<a class="skip" href="#main">')                    # the first stop for the keyboard
        self.assertIn('<main id="main" tabindex="-1">', HTML)
        self.assertRegex(HTML, r'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">')
        self.assertNotRegex(HTML, r"(maximum-scale|user-scalable\s*=\s*no)")
        self.assertIn('<nav aria-label="Sections">', HTML)
        click = JS[JS.index('document.addEventListener("click",e=>{const a=e.target.closest(\'a[href^="#"]\')'):JS.index("tabButtons().forEach(b=>b.addEventListener")]
        self.assertIn('a.classList.contains("skip")', click)                                 # the skip link focuses the content, it is not a route
        self.assertIn('getAttribute("href")==="#"', click)                                   # a link to "#" never rewrites the address (a reload would land on the Dashboard)

    def test_progress_bars_are_named_and_carry_their_value(self):
        bar = JS[JS.index("const bar=(pct,label)"):JS.index("const jobPlain")]
        for needle in ('role="progressbar"', "aria-label=", 'aria-valuemin="0"', 'aria-valuemax="100"', "aria-valuenow="):
            self.assertIn(needle, bar)
        self.assertIn('"Build progress: "+jobPlain(x)', JS)                                 # one name per build, so a list of bars can be told apart
        self.assertIn('"Preparing: "+jobPlain(x)', JS)
        self.assertRegex(HTML, r'<div class="bar" role="progressbar" aria-label="Progress of the run" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">')
        self.assertIn('setAttribute("aria-valuenow",String(pct))', JS)                       # and it moves

    def test_changes_are_announced(self):
        self.assertRegex(HTML, r'id="msg" role="status" aria-live="polite" aria-atomic="true"')   # "Run started", "Scanning…", errors
        self.assertRegex(HTML, r'id="stateWord" role="status" aria-live="polite"')           # Idle / Running / Stopping: only the first words, not the counting
        self.assertRegex(HTML, r'<div id="toast" hidden role="status" aria-live="polite">')
        self.assertRegex(HTML, r'id="setMsg" class="small" role="status" aria-live="polite"')
        self.assertIn('say("Run started")', JS)
        self.assertIn("if(tab!==\"dash\")toast(t,bad)", JS)                                  # a message from another tab is not said into a hidden region
        self.assertIn('id="cfgChanged" hidden role="alert"', HTML)
        state = JS[JS.index("function renderState"):JS.index("function renderRun")]
        self.assertIn('txt.indexOf(" · ")', state)
        self.assertIn("if(ww.textContent!==w)", state)                                       # an unchanged word is not rewritten, so it is not read again every 5 s

    def test_the_dialog_is_modal_named_inert_behind_and_starts_on_the_safe_button(self):
        self.assertRegex(HTML, r'<div class="dlg" role="dialog" aria-modal="true" aria-labelledby="dlgTitle" aria-describedby="dlgBody" tabindex="-1">')
        show = JS[JS.index("const setInert"):JS.index("let dlgDone=null")]
        self.assertIn('"header,main,footer,.skip"', show)
        self.assertIn("x.inert=on", show)
        self.assertLess(show.index("setInert(false);back.hidden=true"), show.index("prev.focus()"))   # the page is live again before focus is put back
        self.assertIn("setInert(true)", show)
        self.assertIn('box.querySelector("button.dng")?box.querySelector("button:not(.dng)")', show)   # a question about something destructive starts on "Cancel"
        self.assertIn('e.key==="Escape"', show)

    def test_the_dashboard_is_patched_not_replaced(self):
        for name, region in (("renderFolders", "#folders"), ("renderAttention", "attention"), ("failBox", "#failBox"), ("renderRun", "#runSub"), ("renderRun", "#runLine"), ("renderLive", "liveBox"),
                             ("renderHealth", "#healthSum"), ("refresh", "#kpis")):
            with self.subTest(name + " " + region):
                start = JS.index("function " + name)
                body = JS[start:start + 6000]
                self.assertIn("morph(", body)
        for bad in ('$("#runSub").innerHTML', '$("#kpis").innerHTML', '$("#healthSum").innerHTML', '$("#failBox").innerHTML', '$("#folders").innerHTML', "box.innerHTML=h", "el.innerHTML=html"):
            self.assertNotIn(bad, JS)
        morph = JS[JS.index("function patchAttrs"):JS.index("function stackify")]
        self.assertEqual(morph.count('a.tagName==="DETAILS"&&'), 2)                          # a section the reader opened is neither closed nor re-opened by a refresh
        self.assertIn("x.nodeValue!==y.nodeValue", morph)                                    # text is changed in place, so a selection in it stays
        self.assertIn("a.replaceChild(y,x)", morph)
        self.assertIn("x.tagName!==y.tagName", morph)

    def test_tables_become_cards_and_keep_their_meaning(self):
        self.assertRegex(HTML, r"@media \(max-width:720px\)\{")
        phone = HTML[HTML.index("@media (max-width:720px)"):HTML.index("@media (max-width:860px)")]
        self.assertIn("main table,main tbody,main tr,main td{display:block;width:100%}", phone)
        self.assertIn("main td[data-label]::before{content:attr(data-label);display:block;", phone)       # a browser without alt text for generated content still shows the name
        self.assertIn("main td[data-label]::before{content:attr(data-label) / \"\"}", phone)  # the label is drawn, not read twice (the header row still names the column)
        for role in ('"table"', '"rowgroup"', '"row"', '"columnheader"', '"cell"'):
            self.assertIn("," + role if role != '"table"' else role, JS[JS.index("function stackify"):JS.index("const num=v=>")])
        self.assertIn("new MutationObserver(()=>stackify($(\"main\")))", JS)                  # every table put in the page, however it got there
        self.assertIn("stackify(t.content)", JS)                                             # and the ones morph builds, before they are compared
        self.assertIn(".nm{overflow-wrap:anywhere}", HTML)
        self.assertIn("main table{overflow-wrap:break-word}", HTML)                           # a column is as wide as its longest unbreakable word (a version): only a name (.nm) may be broken anywhere
        self.assertIn("main{max-width:1200px;margin:0 auto;padding:16px;overflow-wrap:anywhere}", HTML)
        self.assertIn('<span class="nw">${tid} · ${p.version?"v"+esc(p.version)', JS)         # [PPSA…] [v…] on one line
        self.assertIn(".nw{white-space:nowrap}", HTML)

    def test_phones_get_a_bottom_tab_bar_and_every_control_44_px(self):
        phone = HTML[HTML.index("@media (max-width:720px)"):HTML.index("@media (max-width:860px)")]
        self.assertIn("nav{position:fixed;left:0;right:0;bottom:0", phone)
        self.assertIn("min-height:56px", phone)                                              # the bar's tabs: icon and label
        self.assertGreaterEqual(HTML.count("<svg viewBox"), 6)                               # five tab icons and the colour button
        touch = HTML[HTML.index("@media (max-width:900px),(pointer:coarse)"):HTML.index("@media (max-width:720px)")]
        self.assertIn("button.btn,a.btn,.btn.sm,.copy,.iconbtn,.seg .btn{min-height:44px;min-width:44px", touch)
        self.assertIn("select,textarea{min-height:44px;font-size:16px}", touch)              # 16 px: a phone does not zoom into a field
        self.assertIn("input[type=checkbox],input[type=radio]{width:24px;height:24px", touch)
        self.assertIn("summary{min-height:44px", touch)
        self.assertIn("label{min-height:44px", touch)
        self.assertIn("[role=tab]{min-height:44px}", touch)                                   # 721-900 px (a tablet upright) has the same targets, with the tabs in the header
        self.assertIn("(pointer:coarse)", touch)
        self.assertIn("env(safe-area-inset-bottom)", phone)                                   # clear of a phone's home bar

    def test_a_disclosure_has_a_marker_even_when_its_summary_is_a_flex_row(self):
        self.assertIn("summary{list-style:none}summary::-webkit-details-marker{display:none}", HTML)
        self.assertRegex(HTML, r'summary::before\{content:"\\25B8"')
        self.assertRegex(HTML, r'details\[open\]>summary::before\{content:"\\25BE"')

    def test_times_say_the_exact_moment_when_pointed_at(self):
        self.assertIn('<time datetime="${esc(new Date(num(ts)*1000).toISOString())}" title="${esc(exactText(ts))}">', JS)
        self.assertEqual(JS.count("esc(whenText("), 1)                                      # only inside whenHtml itself: every time in the page's HTML is a <time> now
        self.assertIn("(server time", JS)

    def test_the_page_can_be_put_on_a_home_screen(self):
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">', HTML)
        self.assertIn('<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">', HTML)
        self.assertIn('<link rel="icon" type="image/svg+xml" href="/static/icon.svg">', HTML)
        self.assertIn('<meta name="apple-mobile-web-app-capable" content="yes">', HTML)
        self.assertIn('<meta name="mobile-web-app-capable" content="yes">', HTML)


# ------------------------------------------------------------------ what the server hands out

class Served(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config())

    def png_size(self, data):
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        return struct.unpack(">II", data[16:24])

    def test_the_manifest_is_valid_and_its_icons_are_where_it_says(self):
        r = self.client.get("/manifest.webmanifest")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("application/manifest+json"))
        m = r.json()
        self.assertEqual((m["name"], m["display"], m["start_url"], m["scope"]), ("PS5 PKG Station", "standalone", "/", "/"))
        for k in ("short_name", "description", "background_color", "theme_color"):
            self.assertTrue(m[k])
        self.assertTrue(any(i["sizes"] == "192x192" for i in m["icons"]) and any(i["sizes"] == "512x512" for i in m["icons"]))   # what an install prompt asks for
        for icon in m["icons"]:
            with self.subTest(icon["src"]):
                got = self.client.get(icon["src"])
                self.assertEqual(got.status_code, 200)
                self.assertEqual(got.headers["content-type"].split(";")[0], icon["type"])
                if icon["type"] == "image/png":
                    w, h = icon["sizes"].split("x")
                    self.assertEqual(self.png_size(got.content), (int(w), int(h)))
                else:
                    self.assertTrue(got.text.lstrip().startswith("<svg"))
        self.assertEqual(self.png_size(self.client.get("/static/apple-touch-icon.png").content), (180, 180))
        self.assertNotRegex(r.text, r"(?i)password|token|/library|/output")                     # nothing private in a file anyone can fetch

    def test_nothing_else_in_the_static_folder_is_served(self):
        for path in ("/static/index.html", "/static/nothing.png", "/static/../server.py", "/static/%2e%2e/server.py", "/static/icon.svg/x", "/static/"):
            with self.subTest(path):
                self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)                    # the script is still its own route

    def test_the_manifest_may_be_fetched_before_logging_in_and_the_policy_allows_it(self):
        with mock.patch.dict(os.environ, {"PS5PKG_PASSWORD": "correct horse"}):
            from fastapi.testclient import TestClient
            anon = TestClient(self.srv.app)
            r = anon.get("/manifest.webmanifest")
            self.assertEqual(r.status_code, 200)                                              # the browser asks for it with the cookie it has; without one it must not be a login page
            self.assertEqual(anon.get("/static/icon-192.png").status_code, 200)
            self.assertEqual(anon.get("/api/status").status_code, 401)                         # the data is still behind the password
        csp = {d.split()[0]: d.split()[1:] for d in r.headers["content-security-policy"].split(";") if d.strip()}
        self.assertEqual(csp["manifest-src"], ["'self'"])
        self.assertEqual(csp["script-src"], ["'self'"])                                        # and nothing else was loosened
        self.assertEqual(csp["default-src"], ["'none'"])
        self.assertEqual(csp["img-src"], ["'self'", "data:"])

    def test_the_page_itself_is_the_new_markup(self):
        r = self.client.get("/")
        self.assertEqual(r.text, HTML)
        self.assertIn('role="tablist"', r.text)


if __name__ == "__main__":
    unittest.main()
