"""Helpers for displaying assistant messages in a UI."""

from __future__ import annotations

import json
from pathlib import Path

from moveworks_capi import rendering
from moveworks_capi.rendering import (
    Segment,
    citation_label,
    citation_summary,
    clean_label,
    form_link_citations,
    layout_message,
    modal_html_to_markdown,
    normalize_markup,
    strip_flattened_forms,
)

# conftest.py replaces the network lookup for every test; keep the real one.
_REAL_SHORT_LINK_TARGET = rendering.short_link_target

_INTRO = "Here are the Mac steps.\n\nI also found a **Corp VPN** form, which may be relevant ."
_QUESTION = "👉 **Which device are you trying to connect from?**"
_FLATTENED = "\n\n**Corp VPN**\nRemote access to Internal Corporate Systems\n\t\t"

_FORM_CITATION = {
    "citation_id": "cite_form",
    "display": {
        "title": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": "**<https://example.service-now.com/sp?id=sc_cat_item|Corp VPN>**"}},
        "body": {"type": "MARKDOWN_TEXT", "markdown_text": {
            "text": "Remote access to Internal Corporate Systems\n\t\t\nCorp VPN Account\n",
        }},
    },
}
_KB_CITATION = {
    "citation_id": "cite_kb",
    "display": {
        "title": {"markdown_text": {"text": "**<https://example.service-now.com/kb|How to configure VPN>**"}},
        "body": {"markdown_text": {"text": "For Mac OS X..."}},
    },
}


def _message(text: str, refs: list, citations: list | None = None) -> dict:
    return {
        "content": {"type": "MARKDOWN_TEXT", "markdown_text": {"text": text}},
        "citations": [_KB_CITATION, _FORM_CITATION] if citations is None else citations,
        "content_references": refs,
    }


def test_strips_flattened_tail_and_anchors_card_before_question():
    text = f"{_INTRO}\n\n{_QUESTION}{_FLATTENED}"
    refs = [{"citation_id": "cite_kb", "offset": 22}, {"citation_id": "cite_form", "offset": len(_INTRO)}]

    layout = layout_message(_message(text, refs))

    assert layout.removed_citation_ids == ("cite_form",)
    assert layout.segments == (Segment(_INTRO, ("cite_form",)), Segment(_QUESTION))


def test_strip_flattened_forms_keeps_the_question_last():
    text = f"{_INTRO}\n\n{_QUESTION}{_FLATTENED}"

    assert strip_flattened_forms(_message(text, [])) == f"{_INTRO}\n\n{_QUESTION}"
    assert strip_flattened_forms(_message(text, [], citations=[_KB_CITATION])) == text


def test_offset_counts_code_points_not_bytes():
    intro = "It’s on the “Network” pane.\n\nI also found a **Corp VPN** form ."
    text = f"{intro}\n\n{_QUESTION}{_FLATTENED}"

    layout = layout_message(_message(text, [{"citation_id": "cite_form", "offset": len(intro)}]))

    assert layout.segments[0] == Segment(intro, ("cite_form",))


def test_text_without_flattened_tail_is_untouched():
    text = f"{_INTRO}\n\n{_QUESTION}"

    layout = layout_message(_message(text, [{"citation_id": "cite_form", "offset": len(_INTRO)}]))

    assert layout.segments == (Segment(text),)
    assert layout.removed_citation_ids == ()


def test_strips_a_flattened_description_that_wraps_onto_more_lines():
    wrapped = {
        "citation_id": "cite_wrapped",
        "display": {
            "title": {"markdown_text": {"text": "**<https://example.service-now.com/sp?id=sc_cat_item|Password Reset>**"}},
            "body": {"markdown_text": {"text": "Request a reset of a password for a service or an\n\t\t\tapplication.\nMore detail."}},
        },
    }
    text = f"{_INTRO}\n\n**Password Reset**\nRequest a reset of a password for a service or an\n\t\t\tapplication.{_FLATTENED}"

    assert strip_flattened_forms(_message(text, [], citations=[wrapped, _FORM_CITATION])) == _INTRO


def test_tail_that_does_not_match_a_citation_is_kept():
    text = f"{_INTRO}\n\n{_QUESTION}\n\n**Corp VPN**\nSomething the citation does not say"

    layout = layout_message(_message(text, []))

    assert layout.segments == (Segment(text),)


def test_removed_citation_without_reference_ends_up_after_all_text():
    text = f"{_INTRO}\n\n{_QUESTION}{_FLATTENED}"

    layout = layout_message(_message(text, []))

    assert layout.removed_citation_ids == ("cite_form",)
    assert layout.segments == (Segment(f"{_INTRO}\n\n{_QUESTION}"),)


def test_out_of_range_offset_is_ignored():
    text = f"{_INTRO}\n\n{_QUESTION}{_FLATTENED}"

    layout = layout_message(_message(text, [{"citation_id": "cite_form", "offset": 10_000}]))

    assert layout.segments == (Segment(f"{_INTRO}\n\n{_QUESTION}"),)


def test_citation_label_strips_bold_and_slack_link():
    assert citation_label(_FORM_CITATION) == "Corp VPN"
    assert citation_label({"display": {"title": {"markdown_text": {"text": "Plain title"}}}}) == "Plain title"


def _form(cid: str, label: str, first_line: str) -> dict:
    return {
        "citation_id": cid,
        "display": {
            "title": {"markdown_text": {"text": f"**<https://example.service-now.com/{cid}|{label}>**"}},
            "body": {"markdown_text": {"text": f"{first_line}\n{first_line}\nMore detail."}},
        },
    }


def test_forms_sharing_a_list_are_grouped_at_one_position():
    intro = "You can request one of these:\n\n1. **Sales Laptop** \n2. **Standard Laptop** \n3. **Developer Laptop (Mac)** "
    rest = "I also found a warranty article .\n\n👉 **Which laptop type do you need?**"
    flattened = "\n\n**Sales Laptop**\nAcer Aspire NX\n\n**Standard Laptop**\nLenovo - Carbon x1\n\n**Developer Laptop (Mac)**\nMacbook Pro"
    citations = [
        _form("cite_sales", "Sales Laptop", "Acer Aspire NX"),
        _form("cite_std", "Standard Laptop", "Lenovo - Carbon x1"),
        _form("cite_dev", "Developer Laptop (Mac)", "Macbook Pro"),
        _KB_CITATION,
    ]
    refs = [
        {"citation_id": "cite_dev", "offset": intro.index("Developer") + 25},
        {"citation_id": "cite_sales", "offset": intro.index("Sales") + 14},
        {"citation_id": "cite_std", "offset": intro.index("Standard") + 17},
        {"citation_id": "cite_kb", "offset": len(intro) + 34},
    ]

    layout = layout_message(_message(f"{intro}\n\n{rest}{flattened}", refs, citations))

    assert layout.segments == (
        Segment(intro, ("cite_sales", "cite_std", "cite_dev")),
        Segment(rest),
    )


def _titled(text: str) -> dict:
    return {"display": {"title": {"markdown_text": {"text": text}}}}


def test_citation_label_handles_every_title_shape():
    ticket = "<https://x.service-now.com/esc?id=req_details&table=incident&sys_id=fbbd|INC0010037>: connect to vpn"
    problem = "<https://x.service-now.com/esc?id=notifications?view=sp&id=ticket&table=problem&sys_id=fa95|PRB0040001>: Portal won't submit"
    article = "**<https://x.service-now.com/esc?id=kb_article&sys_id=4351|How to configure VPN for Apple Devices\n\t\t:>**"
    wrapped = (
        "**<https://x.service-now.com/esc?id=kb_article&sys_id=e314"
        "|Warranty coverage details for SCI and DeSC\n\t\t\tcomputers\n\t\t:>**"
    )
    question = "**<https://x.service-now.com/esc?id=kb_article&sys_id=931f|What to do when you are locked out of you computer ?:>**"

    assert citation_label(_titled(ticket)) == "INC0010037: connect to vpn"
    assert citation_label(_titled(problem)) == "PRB0040001: Portal won't submit"
    assert citation_label(_titled(article)) == "How to configure VPN for Apple Devices"
    assert citation_label(_titled(wrapped)) == "Warranty coverage details for SCI and DeSC computers"
    assert citation_label(_titled(question)) == "What to do when you are locked out of you computer ?"


def test_normalize_markup_converts_slack_links_emoji_and_marker_gaps():
    text = (
        "Filed **<https://x.service-now.com/esc?a=1&b=2|INC0010037>**: "
        "use **<vpn-nu.example.edu|vpn-nu.example.edu>** :point_right: for your account ."
    )

    assert normalize_markup(text) == (
        "Filed **[INC0010037](https://x.service-now.com/esc?a=1&b=2)**: use **vpn-nu.example.edu** 👉 for your account."
    )


def test_normalize_markup_keeps_space_before_a_shortcode_and_unknown_codes():
    assert normalize_markup("<https://example.com/plain> :made_up_code:") == "https://example.com/plain :made_up_code:"
    assert normalize_markup("ratio 3 :1") == "ratio 3 :1"


def test_clean_label_drops_unmapped_shortcodes_and_collapses_lines():
    assert clean_label(":heavy_check_mark: Approve\n  <https://example.com|now> :made_up:") == "✔️ Approve [now](https://example.com)"


def test_modal_html_to_markdown_keeps_safe_formatting_only():
    html = 'Price: $5<br/>See <a href="https://example.com">docs</a> <b>now</b><p>x</p><script>bad()</script>'

    assert modal_html_to_markdown(html) == "Price: $5\nSee [docs](https://example.com) **now**\nx\nbad()"


def _portal_form_message() -> dict:
    return json.loads((Path(__file__).parent / "fixtures" / "portal_form_message.json").read_text())


# Each short link's one-hop redirect target. The links arrive in a different
# order from the citations they open.
PORTAL_REDIRECTS = {
    "https://app.moveworks.ai/r?v2_fixture_a": "https://example.service-now.com/sp?id=sc_cat_item&sys_id=retirement_inquiry",
    "https://app.moveworks.ai/r?v2_fixture_b": "https://example.service-now.com/sp?id=sc_cat_item&sys_id=enrollment_inquiry",
    "https://app.moveworks.ai/r?v2_fixture_c": "https://example.service-now.com/sp?id=sc_cat_item&sys_id=plan_enrollment",
}


def test_portal_form_links_pair_with_citations_by_redirect_target_not_position():
    paired = form_link_citations(_portal_form_message(), resolve=PORTAL_REDIRECTS.get)
    assert {i: citation_label(c) for i, c in paired.items()} == {
        0: "401(k) Retirement Plan Inquiry",
        1: "401(k) Plan Enrollment Inquiry",
        2: "401(k) Plan Enrollment",
    }


def test_unresolved_portal_links_stay_unpaired():
    assert form_link_citations(_portal_form_message(), resolve=lambda url: None) == {}


def test_citation_summary_is_the_first_body_line():
    citation = _portal_form_message()["citations"][0]
    assert citation_summary(citation) == "Enroll in or modify your 401(k) plan"


def test_short_link_target_only_requests_moveworks_short_links(monkeypatch):
    requested = []
    monkeypatch.setattr(rendering.requests, "get", lambda url, **kw: requested.append(url))
    target = _REAL_SHORT_LINK_TARGET.__wrapped__
    for url in (
        "http://app.moveworks.ai/r?x",
        "https://app.moveworks.ai/login",
        "https://evil.example/r?x",
        "https://moveworks.ai.evil.example/r?x",
    ):
        assert target(url) is None
    assert requested == []
