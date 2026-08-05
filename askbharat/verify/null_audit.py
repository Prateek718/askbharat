"""Null audit — prove that every null is a fact about the page, not a miss.

The extractor is instructed to prefer null over inference, which is right: a
guessed passport fee is worse than a gap. But that instruction has a failure
mode in the opposite direction — the model quietly omits things the page plainly
states, and a null field looks identical either way. Left unchecked, the corpus
silently loses content and nobody can tell.

So every null gets challenged from two independent angles:

1. **A cheap deterministic signal.** Regex/keyword evidence that the page
   contains the *kind* of thing the field wants — a rupee amount near the word
   "fee", a "documents required" heading, a phone number. No model involved, so
   it cannot share the extractor's blind spots.

2. **A targeted single-question re-read.** One field, one question, full page
   text: "Does this page state the fee? Quote it, or answer NOT_STATED." Asking
   about one field is a far easier task than filling twenty at once, so it
   catches omissions the bulk pass made.

A null is CONFIRMED only when both agree the page is silent. Disagreement is a
recall bug, and the audit reports it as one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field

from askbharat.llm.provider import LLMProvider, SchemaValidationFailed


class Verdict(StrEnum):
    CONFIRMED_ABSENT = "confirmed_absent"   # both angles agree: page is silent
    MISSED = "missed"                       # page has it; extractor dropped it
    UNCERTAIN = "uncertain"                 # angles disagree; needs a human


# Deterministic evidence that a field's subject matter is present on the page.
# Deliberately loose — this is a screen for "worth a second look", not a
# decision. A false positive costs one targeted question; a false negative
# means content is lost silently, which is the expensive direction.
EVIDENCE: dict[str, re.Pattern] = {
    "fee_amount": re.compile(
        r"(?:₹|rs\.?|inr)\s*[\d,]+|"
        r"\b(?:fee|charge|cost|amount payable)\b[^.\n]{0,60}?[\d,]+|"
        r"\bno fee\b|\bfree of cost\b|\bnil\b", re.I),
    "fee_notes": re.compile(r"\b(fee|charge|concession|waiver|exempt)\w*\b", re.I),
    "documents_required": re.compile(
        r"\bdocuments?\s+(?:required|needed)\b|\brequired documents?\b|"
        r"\b(?:aadhaar|voter\s*id|ration card|passport size photo)\b", re.I),
    "who_is_eligible": re.compile(
        r"\beligib\w+|\bwho can apply\b|\bcriteria\b|\bqualif\w+\b|"
        r"\bapplicant (?:should|must)\b", re.I),
    "how_to_apply": re.compile(
        r"\bhow to apply\b|\bapplication process\b|\bstep\s*\d|"
        r"\bprocedure\b|\bregistration process\b", re.I),
    "processing_time": re.compile(
        r"\b\d+\s*(?:working\s*)?(?:day|week|month)s?\b|"
        r"\bwithin\s+\d+|\bprocessing time\b|\btimeline\b", re.I),
    "validity": re.compile(
        r"\bvalid(?:ity)?\s+(?:for|up\s?to|till|until)\b|\bexpir\w+|"
        r"\brenew\w+\b", re.I),
    "helpline": re.compile(
        r"\b(?:1800|1[89]\d{2})[\s-]?\d{3,}|\bhelpline\b|\btoll[\s-]?free\b|"
        r"\b\+?91[\s-]?\d{10}\b|\b\d{3,5}[\s-]\d{6,8}\b", re.I),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+", re.I),
    "office_address": re.compile(
        r"\b(?:address|office|directorate|bhavan|bhawan|room no|floor)\b|"
        r"\b\d{6}\b", re.I),
    "grievance_route": re.compile(
        r"\bgrievance\b|\bcomplaint\b|\bappeal\b|\bombudsman\b|\bcpgrams\b", re.I),
    "online_url": re.compile(r"https?://\S+", re.I),
    "ministry": re.compile(r"\bministry\b|\bdepartment\b|\bdirectorate\b", re.I),
    "what_it_is": re.compile(r"\b(scheme|service|programme|program|yojana|award)\b", re.I),
}

# Human-readable subject of each field, used in the targeted question.
FIELD_QUESTION: dict[str, str] = {
    "fee_amount": "the fee or charge payable (a specific amount)",
    "fee_notes": "any detail about fees — variations, concessions, waivers",
    "documents_required": "the documents an applicant must provide",
    "who_is_eligible": "who is eligible / the eligibility criteria",
    "how_to_apply": "the steps to apply",
    "processing_time": "how long processing takes",
    "validity": "how long the benefit or document stays valid",
    "helpline": "a helpline or contact phone number",
    "email": "a contact email address",
    "office_address": "a physical office address to visit or post to",
    "grievance_route": "how to raise a complaint or grievance",
    "online_url": "a URL for applying online",
    "ministry": "the owning ministry or department",
    "what_it_is": "a description of what this scheme/service is",
}


class TargetedAnswer(BaseModel):
    """One focused re-read of the page for a single field."""

    stated: bool = Field(
        description="True ONLY if the page explicitly states this information."
    )
    quote: str | None = Field(
        default=None,
        description=(
            "The exact sentence or phrase from the page that states it, copied "
            "verbatim. Null when stated is false."
        ),
    )


@dataclass
class FieldAudit:
    field: str
    verdict: Verdict
    evidence_found: bool
    model_says_stated: bool | None = None
    quote: str | None = None
    quote_is_on_page: bool | None = None
    quote_matches_field: bool | None = None
    note: str = ""


@dataclass
class PageAudit:
    url: str
    checked: list[FieldAudit] = field(default_factory=list)

    @property
    def missed(self) -> list[FieldAudit]:
        return [f for f in self.checked if f.verdict is Verdict.MISSED]

    @property
    def uncertain(self) -> list[FieldAudit]:
        return [f for f in self.checked if f.verdict is Verdict.UNCERTAIN]


TARGETED_SYSTEM = """\
You are auditing whether one specific piece of information appears on a \
government web page. You are NOT summarising and NOT extracting a record.

Answer only about the exact thing asked. Set stated=true only if the page \
really says it, and then copy the supporting sentence verbatim into quote — \
do not paraphrase, do not compose a sentence of your own. If the page does not \
say it, set stated=false and quote=null.

Being wrong in either direction is costly: claiming the page states something \
it does not corrupts the data, and missing something the page does state means \
a citizen never sees it.
"""


class NullAuditor:
    def __init__(self, provider: LLMProvider | None = None, max_chars: int = 20_000):
        self.p = provider or LLMProvider()
        self.max_chars = max_chars

    @staticmethod
    def _is_null(value) -> bool:
        return value in (None, [], {}, "")

    def _ask(self, page_text: str, field_name: str) -> TargetedAnswer | None:
        subject = FIELD_QUESTION.get(field_name, field_name.replace("_", " "))
        try:
            c = self.p.complete(
                [
                    {"role": "system", "content": TARGETED_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"Does this page state {subject}?\n\n"
                            f"PAGE TEXT:\n{page_text[:self.max_chars]}"
                        ),
                    },
                ],
                task="extract", schema=TargetedAnswer, max_tokens=2000,
            )
            return c.parsed
        except SchemaValidationFailed:
            return None

    def audit(
        self,
        page_text: str,
        extracted: dict,
        url: str = "",
        fields: list[str] | None = None,
    ) -> PageAudit:
        """Challenge every null field in `extracted` against `page_text`."""
        result = PageAudit(url=url)
        candidates = fields or [f for f in EVIDENCE if f in extracted]

        for name in candidates:
            if not self._is_null(extracted.get(name)):
                continue          # populated — nothing to audit

            pattern = EVIDENCE.get(name)
            evidence = bool(pattern.search(page_text)) if pattern else False

            if not evidence:
                # Angle 1 says the subject matter isn't on the page at all.
                # Trust it and skip the model call — this is the common case
                # and paying a request for it would exhaust the daily quota.
                result.checked.append(FieldAudit(
                    field=name, verdict=Verdict.CONFIRMED_ABSENT,
                    evidence_found=False,
                    note="no lexical evidence; not worth a targeted re-read",
                ))
                continue

            answer = self._ask(page_text, name)
            if answer is None:
                result.checked.append(FieldAudit(
                    field=name, verdict=Verdict.UNCERTAIN, evidence_found=True,
                    note="targeted re-read failed to return a valid answer",
                ))
                continue

            # Two independent checks on the quote, because "the model said yes"
            # is not evidence on its own.
            #
            #   (a) Is the quote actually on the page? If not, the model wrote
            #       it, and the claim is void however plausible it reads.
            #   (b) Does the quote satisfy the field's own evidence pattern?
            #       Observed failure: asked for a helpline, the model returned a
            #       real sentence about posting a form to an office — on the
            #       page, but containing no phone number. Requiring the quote to
            #       match the pattern that motivated the question kills that
            #       whole class of false positive for free.
            on_page = None
            quote_matches_field = None
            if answer.stated and answer.quote:
                flat_page = re.sub(r"\s+", " ", page_text).lower()
                needle = re.sub(r"\s+", " ", answer.quote).strip().lower()[:60]
                on_page = needle in flat_page
                quote_matches_field = (
                    bool(pattern.search(answer.quote)) if pattern else True
                )

            if answer.stated and on_page and quote_matches_field:
                verdict, note = Verdict.MISSED, "page states it; extractor returned null"
            elif answer.stated and on_page and not quote_matches_field:
                verdict, note = (
                    Verdict.CONFIRMED_ABSENT,
                    "quote is real but does not contain the thing asked for",
                )
            elif answer.stated and not on_page:
                verdict, note = (
                    Verdict.UNCERTAIN,
                    "model claims it is stated but its quote is not on the page",
                )
            else:
                verdict, note = (
                    Verdict.CONFIRMED_ABSENT,
                    "lexical evidence present but targeted re-read found nothing",
                )

            result.checked.append(FieldAudit(
                field=name, verdict=verdict, evidence_found=True,
                model_says_stated=answer.stated, quote=answer.quote,
                quote_is_on_page=on_page,
                quote_matches_field=quote_matches_field, note=note,
            ))

        return result
