"""Mail classification stays fresh without repeatedly scanning large cached bodies."""

import pytest

from coursedeck import mail as mail_module
from coursedeck.mail import MailMessage, MailRule, MailStore, matches


@pytest.mark.parametrize(
    ("needle", "text", "expected"),
    [
        ("art", "department", False),
        ("art", "fine art!", True),
        ("art", "art_course", False),
        ("art", "éart", False),
        ("art", "美art", False),
        ("ART", "ＡＲＴ", True),
        ("strasse", "Straße", True),
        ("café", "cafe\u0301", True),
        ("action required", "Action\u00a0\nrequired", True),
        ("C++", "C++ assignment", True),
        ("C++", "C++assignment", False),
    ],
)
def test_fast_rejection_preserves_unicode_word_boundaries(needle, text, expected):
    assert matches(needle, text) is expected


def test_many_rules_and_courses_scan_large_body_once(monkeypatch):
    message = MailMessage(
        id="large",
        sender="Instructor",
        subject="Calculus II",
        body=("Lecture notes with Unicode café and numbers 123. " * 4000)
        + "Action required: finish this survey.",
        body_complete=True,
    ).model_dump()
    courses = [{"id": str(i), "name": f"Unrelated course {i}"} for i in range(40)]
    courses.append({"id": "calculus", "name": "Calculus II"})
    rules = [
        MailRule(field="any", contains=f"absent keyword {i}", action="ignore").model_dump()
        for i in range(30)
    ]
    rules.extend(rule.model_dump() for rule in mail_module.DEFAULT_MAIL_RULES.values())
    normalize = mail_module.normalized
    large_scans = []

    def counted(value):
        if len(value) > 10000:
            large_scans.append(len(value))
        return normalize(value)

    monkeypatch.setattr(mail_module, "normalized", counted)
    result = MailStore(None).classify(message, {}, courses, rules)
    assert result["course_id"] == "calculus"
    assert result["categories"] == ["Action required", "Survey"]
    assert result["attention"]
    assert not result["ignored"]
    assert len(large_scans) == 1


def test_normalized_field_reuse_preserves_sender_body_and_priority_semantics():
    store = MailStore(None)
    message = MailMessage(
        id="fields",
        sender="Instructor",
        sender_email="Calculus II",
        subject="  ＡＲＴ   update ",
        body="No survey required. Action required: respond.",
        body_complete=True,
    ).model_dump()
    courses = [{"id": "calculus", "name": "Calculus II"}]
    rules = [
        MailRule(
            field="subject", contains="art update", match="equals", action="category", value="Art"
        ).model_dump(),
        MailRule(field="sender", contains="calculus", action="filter").model_dump(),
        *[rule.model_dump() for rule in mail_module.DEFAULT_MAIL_RULES.values()],
    ]
    result = store.classify(message, {}, courses, rules)
    # Sender text participates in rules, but does not imply the course by its name.
    assert result["classification"] == "none"
    assert result["categories"] == ["Action required", "Art"]
    assert result["ignored"]
    assert result["attention_reasons"] == ["Action required"]

    message.update(body_stale=True, body_complete=False)
    result = store.classify(message, {"ignored": False}, courses, rules)
    assert result["classification"] == "unclassifiable"
    assert result["categories"] == ["Art"]
    assert not result["attention"]
    assert not result["ignored"]
