"""The coercion layer in ExtractedService, and why each case is there.

Every case below was observed coming back from a free model during the
validation batches. Each one used to cost a corrective request against a
metered daily quota; together they were the difference between ~3.7 and ~1.3
requests per page, which on a 4,721-page corpus is several days of wall clock.

The line these tests defend: repackaging what the model said is allowed,
inventing what it did not say is not. Nothing here fabricates content.
"""
from __future__ import annotations

import pytest

from askbharat.llm.provider import _strip_fences
from askbharat.schema.extraction import ExtractedService


def parse(**kw) -> ExtractedService:
    return ExtractedService.model_validate(kw)


class TestListCoercion:
    def test_prose_becomes_single_item(self):
        assert parse(documents_required="Aadhaar card").documents_required == [
            "Aadhaar card"
        ]

    def test_newline_separated_becomes_items(self):
        rec = parse(documents_required="Aadhaar\nRation card\nIncome certificate")
        assert rec.documents_required == [
            "Aadhaar", "Ration card", "Income certificate",
        ]

    def test_bullets_become_items(self):
        rec = parse(how_to_apply="- Register online\n- Upload documents")
        assert rec.how_to_apply == ["Register online", "Upload documents"]

    def test_inline_numbered_steps_split(self):
        rec = parse(how_to_apply="1. Fill the form 2. Submit it 3. Collect receipt")
        assert rec.how_to_apply == [
            "Fill the form", "Submit it", "Collect receipt",
        ]

    def test_non_sequential_numbers_do_not_split(self):
        """'Form 3. and Form 7.' is one requirement, not two steps."""
        rec = parse(documents_required="Form 3. and Form 7. required")
        assert rec.documents_required == ["Form 3. and Form 7. required"]

    def test_decimal_amount_is_not_split(self):
        """A fee like 'Rs. 2. 5 lakh' must survive intact."""
        rec = parse(documents_required="Rs. 2. 5 lakh receipt")
        assert rec.documents_required == ["Rs. 2. 5 lakh receipt"]

    def test_dict_keeps_its_grouping_as_a_prefix(self):
        rec = parse(documents_required={
            "application_stage": ["Form A", "Photo"],
            "verification": "Aadhaar",
        })
        assert rec.documents_required == [
            "application stage: Form A",
            "application stage: Photo",
            "verification: Aadhaar",
        ]

    def test_list_of_dicts_is_flattened_without_loss(self):
        rec = parse(documents_required=[
            {"name": "Aadhaar", "note": "self-attested"},
            {"name": "Photo"},
        ])
        assert rec.documents_required == ["Aadhaar — self-attested", "Photo"]

    def test_already_correct_list_is_untouched(self):
        rec = parse(documents_required=["Aadhaar", "PAN"])
        assert rec.documents_required == ["Aadhaar", "PAN"]

    def test_empty_string_is_empty_list(self):
        assert parse(documents_required="").documents_required == []


class TestTextCoercion:
    def test_list_of_conditions_becomes_prose(self):
        rec = parse(who_is_eligible=["Resident of Haryana", "Age 60 or above"])
        assert rec.who_is_eligible == "Resident of Haryana; Age 60 or above"

    def test_contact_dict_keeps_every_value(self):
        """helpline arriving as a dict must not silently drop the number."""
        rec = parse(helpline={"phone_number": "8638922415", "email": "x@nic.in"})
        assert "8638922415" in rec.helpline
        assert "x@nic.in" in rec.helpline

    def test_plain_string_is_untouched(self):
        assert parse(who_is_eligible="Anyone").who_is_eligible == "Anyone"

    def test_empty_list_becomes_none_not_empty_string(self):
        assert parse(who_is_eligible=[]).who_is_eligible is None


class TestOptionalIdentity:
    def test_empty_payload_validates(self):
        """No field is required; the pipeline supplies the name it already knows."""
        rec = parse()
        assert rec.canonical_name is None
        assert rec.page_is_about_a_service is True

    def test_nulls_are_preserved_not_invented(self):
        rec = parse(fee_amount=None, processing_time=None)
        assert rec.fee_amount is None
        assert rec.processing_time is None


class TestJSONRecovery:
    @pytest.mark.parametrize("raw,expected", [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ('**Extracted Fields**\n\n{"a": 1}\n\nNo estimates made.', '{"a": 1}'),
        ('{"a": {"b": "}"}, "c": 2}', '{"a": {"b": "}"}, "c": 2}'),
    ])
    def test_object_is_recovered(self, raw, expected):
        assert _strip_fences(raw) == expected

    def test_brace_inside_string_does_not_end_the_object(self):
        out = _strip_fences('note\n{"a": "closing } here", "b": 1}\ntail')
        assert out == '{"a": "closing } here", "b": 1}'

    def test_response_with_no_json_is_returned_unchanged(self):
        assert _strip_fences("I cannot answer") == "I cannot answer"
