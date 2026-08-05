"""The LLM-facing extraction schema.

Deliberately **flat** and separate from `ServiceRecord`:

- Free models are markedly less reliable with `$ref`/`$defs` nesting than
  frontier models, and several providers reject or mangle deep schemas under
  `strict: true`. Lists of strings survive where lists of objects do not.
- The model must not invent `id`, `sources`, or `field_provenance` — those are
  facts about *our* pipeline, not about the page. The pipeline assigns them.

Every content field is `| None`. That is the whole point: we are measuring
whether the model will say "the page doesn't state this" instead of producing a
plausible number. A confident guess about a passport fee is worse than a gap,
because a gap can be surfaced honestly and a wrong fee cannot be detected by a
citizen.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Free models get the *content* right and the *container* wrong: eligibility
# comes back as a list of conditions when the schema asks for prose, and
# application steps come back as one numbered string when the schema asks for a
# list. Under `strict: true` both are validation failures, and each failure
# costs a corrective request — measured at roughly 3.7 requests per page, most
# of them spent re-asking for a shape rather than a fact.
#
# Coercing here rather than retrying is not laxness. The model's answer was
# correct; only its packaging was wrong, and we can repackage it losslessly.
# What we refuse to do is invent content, which no validator below does.

_BULLET_SPLIT = re.compile(r"(?:^|\n)\s*(?:\d+[.)]|[-*•])\s+")
_NUMBERED = re.compile(r"(?:(?<=\s)|^)(\d{1,2})[.)]\s+")


def _split_numbered(s: str) -> list[str] | None:
    """Split '1. a 2. b 3. c' — but only when the numbers really run 1,2,3.

    Naively splitting on any 'N.' turns a fee like 'Rs. 2. 5 lakh' into
    fragments. Requiring a full sequence from 1 makes a false positive
    essentially impossible, and a genuine step list always satisfies it.
    """
    marks = list(_NUMBERED.finditer(s))
    if len(marks) < 2:
        return None
    if [int(m.group(1)) for m in marks] != list(range(1, len(marks) + 1)):
        return None
    parts = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(s)
        piece = s[m.end():end].strip(" \t\n;,")
        if piece:
            parts.append(piece)
    return parts or None


def _flatten(v) -> list[str]:
    """Flatten one level of nesting into plain strings."""
    out: list[str] = []
    for item in v:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            # {'name': 'Aadhaar', 'note': 'self-attested'} -> 'Aadhaar — self-attested'
            vals = [str(x) for x in item.values() if isinstance(x, (str, int, float))]
            if vals:
                out.append(" — ".join(vals))
        elif isinstance(item, list):
            out.extend(_flatten(item))
        elif item is not None:
            out.append(str(item))
    return out


def _to_list(v):
    """A string that is really a list -> a list. Anything else -> unchanged."""
    if v is None:
        return v
    if isinstance(v, list):
        # Elements may themselves be dicts or nested lists.
        return _flatten(v) if any(not isinstance(x, str) for x in v) else v
    if isinstance(v, dict):
        # Grouped output, e.g. documents_required keyed by application stage.
        # The grouping is real information, so keep the key as a prefix rather
        # than discarding it.
        out: list[str] = []
        for key, val in v.items():
            label = str(key).replace("_", " ").strip()
            items = _flatten(val) if isinstance(val, (list, dict)) else [str(val)]
            out.extend(f"{label}: {i}" if label else i for i in items)
        return out
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        parts = [p.strip() for p in _BULLET_SPLIT.split(s) if p.strip()]
        if len(parts) > 1:
            return parts
        numbered = _split_numbered(s)
        if numbered:
            return numbered
        parts = [p.strip() for p in s.split("\n") if p.strip()]
        return parts if len(parts) > 1 else [s]
    return v


def _to_text(v):
    """A list or dict that is really prose -> joined prose. Else unchanged."""
    if isinstance(v, list):
        items = [str(x).strip() for x in _flatten(v) if str(x).strip()]
        if not items:
            return None
        # Semicolons keep the original boundaries legible instead of welding
        # separate conditions into one run-on sentence.
        return "; ".join(items)
    if isinstance(v, dict):
        # e.g. helpline arriving as {'phone_number': '...', 'email': '...'}.
        # Joining keeps every value the page actually stated; dropping the
        # dict would silently lose a contact number a citizen needs.
        parts = [str(x).strip() for x in v.values()
                 if isinstance(x, (str, int, float)) and str(x).strip()]
        return "; ".join(parts) or None
    return v


class ExtractedService(BaseModel):
    """What one government web page says about one service."""

    @field_validator("colloquial_names", "documents_required", "how_to_apply",
                     "application_modes", "fields_not_stated", mode="before")
    @classmethod
    def _coerce_list(cls, v):
        return _to_list(v)

    @field_validator("what_it_is", "who_is_eligible", "fee_notes",
                     "processing_time", "validity", "grievance_route",
                     "office_address", "helpline", "email", "ministry", "exclusions",
                     "state", "kind", "jurisdiction_level", "canonical_name",
                     "online_url", mode="before")
    @classmethod
    def _coerce_text(cls, v):
        return _to_text(v)

    # --- identity ---------------------------------------------------------
    # Optional, despite being the one field every record needs. Free providers
    # honour `strict: true` loosely and drop required fields, and each drop
    # costs a corrective retry against a metered quota — measured at roughly
    # one wasted request per page. We already hold the authoritative title in
    # `scheme_catalogue`, so the pipeline fills this in when the model omits
    # it. Asking the model for a fact we already know was buying nothing.
    canonical_name: str | None = Field(
        default=None,
        description="Official name of the service or scheme, as the page states it."
    )
    colloquial_names: list[str] = Field(
        default_factory=list,
        description=(
            "Everyday names a citizen might use, ONLY if the page shows them. "
            "Do not invent nicknames."
        ),
    )
    kind: str | None = Field(
        default=None,
        description="One of: service, scheme, document, certificate, grievance.",
    )

    # --- jurisdiction -----------------------------------------------------
    jurisdiction_level: str | None = Field(
        default=None, description="One of: central, state, district."
    )
    state: str | None = Field(
        default=None,
        description="State/UT name if this is state-specific; null if national.",
    )
    ministry: str | None = Field(
        default=None, description="Owning ministry or department, verbatim."
    )

    # --- the answer -------------------------------------------------------
    what_it_is: str | None = Field(
        default=None, description="2-3 plain sentences describing what this is."
    )
    who_is_eligible: str | None = Field(
        default=None,
        description="Eligibility as the page states it. Quote conditions closely.",
    )
    # myScheme renders "Exclusions" as its own section on 762 of 4,721 pages,
    # and folding it into who_is_eligible loses it: a model summarising
    # eligibility tends to report who qualifies and drop who is barred. The
    # asymmetry matters — being wrongly told you qualify costs a citizen a
    # rejected application and a wasted trip, so a disqualifier is worth more
    # than the rule it contradicts.
    exclusions: str | None = Field(
        default=None,
        description=(
            "Who is explicitly NOT eligible, or conditions that disqualify an "
            "applicant. Often under an 'Exclusions' heading. Null if the page "
            "states no disqualifying conditions."
        ),
    )
    documents_required: list[str] = Field(
        default_factory=list,
        description="Each required document, one per item, exactly as listed.",
    )
    fee_amount: float | None = Field(
        default=None,
        description=(
            "Numeric fee in INR if the page states a single specific amount. "
            "Null if the page does not state a fee, or if it varies."
        ),
    )
    fee_notes: str | None = Field(
        default=None,
        description="Fee detail: variations, categories, waivers, concessions.",
    )
    how_to_apply: list[str] = Field(
        default_factory=list,
        description="Ordered application steps, one per item.",
    )
    application_modes: list[str] = Field(
        default_factory=list,
        description="Any of: online, offline, csc, post. Only what the page states.",
    )
    online_url: str | None = Field(
        default=None, description="Direct URL to apply online, if the page gives one."
    )
    processing_time: str | None = Field(
        default=None, description="Stated processing/turnaround time."
    )
    validity: str | None = Field(
        default=None, description="How long the document/benefit remains valid."
    )
    helpline: str | None = Field(
        default=None,
        description=(
            "Any contact phone number for this service — a toll-free helpline, "
            "or a plain office landline such as '0832-2404640'. Not only "
            "numbers labelled 'helpline'."
        ),
    )
    # Added after a null audit: a Goa scheme page listed
    # 'Phone: 0832-2404640, Email: aco3-dac.goa@nic.in' and the email had
    # nowhere to go, so it was being discarded on every page that had one.
    email: str | None = Field(
        default=None, description="Contact email address for this service, if stated."
    )
    office_address: str | None = Field(
        default=None,
        description="Physical office/where-to-submit address, if the page gives one.",
    )
    grievance_route: str | None = Field(
        default=None, description="How to complain or escalate, if stated."
    )

    # --- honesty signals --------------------------------------------------
    # These exist so the pipeline can tell "page had nothing" apart from
    # "model didn't try", and so a low-signal page can be dropped rather than
    # silently contributing an empty record.
    # Defaulted for the same reason as canonical_name: a required field the
    # model may drop costs a retry. True is the safe default here because the
    # corpus is scheme pages — the exception is worth a token, the rule is not.
    page_is_about_a_service: bool = Field(
        default=True,
        description=(
            "False if this page is a listing, news item, error page, or "
            "otherwise not about one specific service."
        )
    )
    fields_not_stated: list[str] = Field(
        default_factory=list,
        description=(
            "Names of fields you set to null BECAUSE the page does not state "
            "them. This is the expected case — most pages state very little."
        ),
    )
