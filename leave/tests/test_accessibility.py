"""Regression guards for accessibility that do not need a browser.

These do not replace a real screen-reader and keyboard check, but they catch the mistakes that
creep back in: unlabelled fields, ambiguous repeated links, colour-only status, low-contrast colours.
"""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from leave import services

from .conftest import TODAY, day

pytestmark = pytest.mark.usefixtures("frozen_today")

GENERIC = {"open", "manage", "view", "edit", "remove", "delete", "cancel"}
NO_LABEL_TYPES = {"hidden", "submit", "button", "reset", "image"}


class Page(HTMLParser):
    """Collects what the checks need from one HTML page."""

    def __init__(self):
        super().__init__()
        self.ids, self.h1 = [], 0
        self.labels_for, self.wrapping_label_depth = set(), 0
        self.fields, self.th_without_scope, self.links = [], [], []
        self._link = None
        self.has_main = False
        self.first_link = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "h1":
            self.h1 += 1
        if tag == "main" and a.get("id") == "main":
            self.has_main = True
        if tag == "label":
            self.wrapping_label_depth += 1
            if "for" in a:
                self.labels_for.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type", "text") not in NO_LABEL_TYPES:
            self.fields.append((tag, a, self.wrapping_label_depth > 0))
        if tag == "th" and "scope" not in a:
            self.th_without_scope.append(a)
        if tag == "a":
            if self.first_link is None:
                self.first_link = a.get("href")
            self._link = {"text": "", "aria": a.get("aria-label")}
        if tag == "button" and "link" in a.get("class", ""):
            self._link = {"text": "", "aria": a.get("aria-label")}

    def handle_endtag(self, tag):
        if tag == "label":
            self.wrapping_label_depth -= 1
        if tag in ("a", "button") and self._link is not None:
            self.links.append(self._link)
            self._link = None

    def handle_data(self, data):
        if self._link is not None:
            self._link["text"] += data


def parse(html: str) -> Page:
    page = Page()
    page.feed(html)
    return page


@pytest.fixture
def world(manager, admin_user, team, roles, techs, vacation):
    leave = services.book_leave(user=techs[0], leave_type=vacation, start_date=day(0), end_date=day(1), today=TODAY)
    services.set_month_floor(actor=manager, team=team, resource_role=roles["Technical"], year=2026, month=12, min_available=3)
    services.set_floor_override(actor=manager, team=team, resource_role=roles["Technical"], start_date=day(2), end_date=day(2), min_available=1)
    return leave


def pages_for(leave, manager, admin_user, techs):
    return [
        (techs[0], reverse("calendar")), (techs[0], reverse("book-leave")), (techs[0], reverse("my-leave")),
        (manager, reverse("approvals")), (manager, reverse("log-leave")),
        (manager, reverse("floors") + "?month=2026-12"), (manager, reverse("breaches")),
        (manager, reverse("members")), (manager, reverse("member-detail", args=[techs[0].pk])),
        (manager, reverse("settings")), (manager, reverse("audit")),
        (manager, reverse("request-detail", args=[leave.pk])), (admin_user, reverse("console")),
    ]


def test_every_screen_meets_the_basics(client, world, manager, admin_user, techs):
    for user, url in pages_for(world, manager, admin_user, techs):
        client.force_login(user)
        response = client.get(url)
        assert response.status_code == 200, url
        html = response.content.decode()
        page = parse(html)
        assert page.h1 == 1, f"{url}: needs exactly one h1, has {page.h1}"
        assert page.has_main, f"{url}: no main landmark to skip to"
        assert page.first_link == "#main", f"{url}: the skip link must be the first link"
        assert len(page.ids) == len(set(page.ids)), f"{url}: duplicate ids {[i for i in page.ids if page.ids.count(i) > 1]}"
        assert not page.th_without_scope, f"{url}: table headers need scope"
        for tag, attrs, wrapped in page.fields:
            labelled = wrapped or attrs.get("aria-label") or attrs.get("aria-labelledby") or attrs.get("id") in page.labels_for
            assert labelled, f"{url}: unlabelled {tag} {attrs}"
        assert 'lang="en"' in html


def test_repeated_actions_say_what_they_act_on(client, world, manager, admin_user, techs):
    """A bare "Open" that appears more than once on a page tells a screen-reader user nothing."""
    for user, url in pages_for(world, manager, admin_user, techs):
        client.force_login(user)
        links = parse(client.get(url).content.decode()).links
        bare = [l["text"].strip().lower() for l in links if l["text"].strip().lower() in GENERIC and not l["aria"]]
        repeated = {t for t in bare if bare.count(t) > 1}
        assert not repeated, f"{url}: repeated links without context: {repeated}"


def test_current_page_is_marked_in_the_navigation(client, world, manager):
    client.force_login(manager)
    body = client.get(reverse("floors")).content.decode()
    assert re.search(r'href="/manage/floors/"[^>]*aria-current="page"', body)
    assert not re.search(r'href="/manage/breaches/"[^>]*aria-current', body)


def test_floor_status_is_not_conveyed_by_colour_alone(client, world, manager):
    client.force_login(manager)
    body = client.get(reverse("floors") + "?month=2026-12").content.decode()
    assert "Below floor" in body  # a day under its floor says so in words
    assert "At the floor" in body or "Cover holds" in body  # the other states have text for screen readers


# ---- colour contrast of the palette in base.html -------------------------------------------------- #
def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _palettes():
    css = (Path(settings.BASE_DIR) / "templates" / "base.html").read_text()
    blocks = re.findall(r":root \{(.*?)\}", css, flags=re.S)
    assert len(blocks) == 2, "expected a light and a dark :root block"
    return [dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", b)) for b in blocks]


PAIRS = [  # (foreground, background): text and controls the pages actually use
    ("text", "bg"), ("text", "surface"), ("muted", "bg"), ("muted", "surface"), ("accent", "bg"), ("accent", "surface"),
    ("accent-text", "accent"), ("muted", "nofloor"), ("muted", "off"), ("text", "ok"), ("text", "low"), ("text", "full"),
    ("text", "nofloor"), ("full-b", "surface"), ("ok-b", "surface"), ("low-t", "surface"), ("low-t", "bg"),
    ("accent", "low"), ("accent", "ok"), ("accent", "full"), ("accent", "nofloor"), ("muted", "low"),
]


@pytest.mark.parametrize("index,name", [(0, "light"), (1, "dark")])
def test_palette_meets_wcag_aa_for_text(index, name):
    palette = _palettes()[index]
    failures = [
        f"{fg} on {bg} = {_ratio(palette[fg], palette[bg]):.2f}"
        for fg, bg in PAIRS
        if _ratio(palette[fg], palette[bg]) < 4.5
    ]
    assert not failures, f"{name} theme below 4.5:1: {failures}"
