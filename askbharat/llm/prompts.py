"""Prompts, kept in one place so the spike and the production runner cannot drift.

The extraction prompt was tuned against hand-scored spike output. Its shape is
deliberate: it names *two* failure modes rather than one. An earlier version
warned only against inventing facts, and the model responded by nulling almost
everything — which scores perfectly on hallucination and produces a useless
product. Both failures have to be named for the model to aim between them.
"""
from __future__ import annotations

EXTRACTION_SYSTEM = """\
You extract structured facts from Indian government web pages for a citizen \
information service. A citizen will act on what you output.

There are two ways to fail here, and they are equally bad:

  A. Writing something the page does not say (a guessed fee, an assumed
     eligibility rule). This misleads the citizen.
  B. Leaving out something the page DOES say. This makes the service useless
     and sends the citizen back to the maze we are replacing.

Avoid both. Concretely:

1. EXTRACT WHAT IS THERE. If the page describes what the service is, fill
   what_it_is. If it lists documents, fill documents_required. If it explains
   who qualifies, fill who_is_eligible. Do not leave a field null because the
   page's wording is informal, scattered across sections, or not under a
   heading matching the field name. Read the whole page and pull out what it
   actually tells a citizen.

2. NEVER INVENT. If the page does not state a fee, fee_amount is null. Do not
   estimate from general knowledge, do not carry a number over from a different
   service mentioned on the page, do not convert "nominal fee" into a figure.
   Null is the correct, expected answer for anything the page is silent on.

3. Quote closely. For eligibility and documents, stay near the page's wording
   rather than paraphrasing into something more confident than the source.

4. In fields_not_stated, list the fields you left null because the page is
   genuinely silent on them.

5. Set page_is_about_a_service to false ONLY if the page is a listing, index,
   news item, error page, or department homepage with no service description.
   A page that describes a scheme, award, benefit, or process IS a service page
   even if it has no application form on it.

Extract from the page normally and write the extracted values in English.
"""

# myScheme pages are rendered from one template with named sections, so the
# model can be told where to look. This is appended, not substituted — the
# rules above still govern.
MYSCHEME_HINT = """\

This page comes from myScheme (myscheme.gov.in) and follows a fixed template.
Expect these sections, and map them accordingly:

  Details              -> what_it_is (and often ministry / state)
  Benefits             -> what the citizen receives; fold into what_it_is or
                          fee_notes if it describes amounts
  Eligibility          -> who_is_eligible. If this section has ANY content,
                          who_is_eligible must not be null. Measured on live
                          output, 12% of records left it null while the page
                          listed conditions in full — the model had summarised
                          them into what_it_is and considered the job done.
                          Putting a fact in a neighbouring field is the same
                          failure as dropping it: the site then tells the
                          citizen "the official page does not state
                          eligibility" about a page that states it plainly.
                          Fill BOTH where both apply; they are not exclusive.
  Exclusions           -> exclusions. A separate section on many pages. Never
                          merge it into who_is_eligible — a citizen who reads
                          only who qualifies and misses who is barred applies
                          and is rejected.
  Application Process  -> how_to_apply, application_modes
  Documents Required   -> documents_required
  Frequently Asked Questions -> often the ONLY place that states fees,
                          processing time, helpline, office address and
                          grievance route. Read it in full; do not skim it as
                          boilerplate.

There is usually a "Grievance Redressal" block naming an officer, an email or a
portal. That is grievance_route — fill it when it is there.

Watch one specific trap. A stated turnaround is only processing_time if it is
the time to process an *application*. Grievance sections routinely promise a
reply "within 30 days"; that is the complaint window, not the application
turnaround, and copying it into processing_time invents a fact the page never
claimed. When a duration appears, check which process it belongs to.

The section headings are always present even when a section is nearly empty, so
the presence of a heading is not evidence that the fact exists. Judge by the
content under it.

These are central and state government schemes, not transactional services.
kind is almost always "scheme". If the page names a state in its eligibility or
title, set state and jurisdiction_level="state"; a scheme open to all of India
is jurisdiction_level="central" with state=null.
"""
