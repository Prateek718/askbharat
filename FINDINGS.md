# india.gov.in — Structural & UX Audit

**Audited:** 27 July 2026 · live site (`www.india.gov.in`, currently badged **BETA**)
**Method:** direct navigation + DOM inspection of the rendered SPA; all numbers below were read off live pages.

---

## TL;DR

Your instinct is right, but the diagnosis is more specific than "it's a maze."

india.gov.in is **a link catalogue wearing the costume of a service portal.** The navigation is not badly *labelled* — the 18-category taxonomy is genuinely reasonable. The problem is that every path through it terminates in a stub page that hands you off to another website, and that most of the content the site advertises cannot be reached by browsing at all.

The redesign has fixed the *look*. It has not fixed the *information architecture*.

---

## 1. The actual site structure

Top level (from `/site-map`):

| Section | Contents |
|---|---|
| **Category** | 18 topic categories → subcategories → item lists |
| **Services** | A–Z "Important Services" (33 marquee items) + 13,819 service records |
| **My-Government** | Acts & Rules, Schemes, Constitution of India, Documents |
| **Directory** | Who's Who, Contact Directory, Web Directory, Public Utilities, Helpline |
| **Explore India** | Tourist Places, Culinary Delights, One District One Product, Facts of India |
| **News Hub** | Doordarshan, PIB press releases, PIB photos, News on AIR |
| **Others** | About, Contact, Feedback, FAQs, Help, Calendar, Spotlights, policies |

Headline counters on the homepage: **13,819** online services · **5,456** schemes · **2,281** citizen engagements · **3,897** tourist places · **1,207** ODOP products · **18** categories.

The 18 categories are task-oriented and defensible:

> Agriculture Rural & Environment · Benefits & Social Development · Business & Self-employed · Citizenship Visa & Passports · Defence & Foreign Affairs · Driving & Transport · Education & Learning · Governance & Planning · Health & Wellness · Housing & Local Services · Infrastructure & Industries · Jobs · Justice Law & Grievances · Money & Taxes · Science IT & Communication · Travel & Tourism · Welfare of Families · Youth Sports & Culture

**This taxonomy is worth keeping.** The failure is below it.

---

## 2. Finding: every journey dead-ends in a stub

This is the central structural problem.

Walk the flagship path — apply for a passport:

```
Home → Services → Passport (accordion) → Ordinary Passport → detail page → passportindia.gov.in
```

Five steps. And the detail page at the end (`/services/details/passport-seva-apply-for-ordinary-passport`) contains, in full:

- the service title
- the owning ministry ("Ministry of External Affairs")
- a delivery-mode badge ("Fully Online")
- **one paragraph** of description
- `For More Information Visit → https://www.passportindia.gov.in/psp/Apply`
- a feedback form **behind a CAPTCHA**

That's it. No eligibility criteria. No document checklist. No fees. No processing time. No steps. No "what happens next."

I checked a second leaf (`.../passport-seva-check-passport-appointment-availability`) — byte-for-byte the same template.

**So the portal's job, as currently built, is to tell you that a website exists.** A user who already knows they need a passport learns nothing they couldn't get from one Google search — and pays five clicks for it.

> **This is the single thing your project should fix.** The leaf page is where a citizen's question actually gets answered, and right now it is empty.

---

## 3. Finding: ~84% of content is unreachable by browsing

Take **Health & Wellness**. The category page advertises:

| Advertised on category page | Count |
|---|---|
| Schemes | 322 |
| Services | 588 |
| Open Data | 2,159 |
| Activities | 1 |
| **Total** | **3,070** |

Now walk into its four subcategories and read the actual item counts:

| Subcategory | Items |
|---|---|
| Health Resources | 359 |
| Health Care Promotion & Products | 118 |
| Children's Health & Immunisation | 13 |
| **Disease & Conditions** | **2** |
| **Total browsable** | **492** |

**492 reachable vs 3,070 advertised — 84% of the category is invisible to a browsing user.** Even if you discount Open Data as living on data.gov.in, it's 492 of 911 — still nearly half missing.

And look at that last row. **"Disease & Conditions" — in a country of 1.4 billion people — contains two items:**

1. *"Information of Ayurveda"* — a philosophical gloss on the etymology of the word, quoting Charaka Sutra 1-4
2. *"Chat with Doctors"* — a Tamil Nadu state service

Meanwhile "Health Resources" (359 items) is dominated by **CGHS reimbursement forms for central government employees** — not public health information. The label promises citizen health guidance; the drawer contains staff paperwork.

The subcategory pages themselves close with:

> *"Could not find what you were looking for? — Post Your Suggestion"*

**The site knows its navigation fails, and has installed a complaints box instead of fixing it.**

---

## 4. Finding: search is a keyword index, not an answer engine

Search lives at `/search?search=<query>`. It matches strings. It does not model intent.

**Query: `birth certificate` → 3,685 results.** First page includes:

- *George Orwell Birth Place* — a tourist attraction
- *Dr. Babasaheb Ambedkar, Birth Place* — a tourist attraction
- then Kohima, Ahmedabad, Chhattisgarh, Goa, Assam certificate services, interleaved at random

It matched the token "Birth" and served up literary tourism to someone registering a newborn.

**Query: `I lost my ration card` → 447 results.** Not one addresses replacement. Top hits are the Yellow Ration Card food scheme and One Nation One Ration Card. The words "lost", "duplicate", "replace" are simply discarded.

**Query: `Apply for Driving Licence` → 256 results**, mixing Kerala, Telangana, Haryana and West Bengal services with no way to narrow down.

### The filter facets are the real scandal

The entire filter panel offers **three options**:

```
Catalogues  (2)
Services    (233)
Web Pages & Documents  (21)
```

**There is no state filter.** Driving licences, ration cards, birth certificates, land records — these are *state subjects*. Their delivery is state-specific by constitutional design. A citizen in Odisha must manually scan 256 results to find the Odisha one.

The maddening part: the **subcategory** pages *do* have a `All / Central / State` radio filter. The capability exists in the codebase. It just isn't wired into the one place users need it.

Also spotted in live results: **"Apply for a Learnerrsquo;s Driving Licence"** — an unescaped `&rsquo;` HTML entity leaking into production search titles.

---

## 5. Finding: four parallel taxonomies for the same reality

The same government service is filed under multiple incompatible schemes, and search results expose the seams — a single result page returns items tagged `Category:- Services`, `Category:- Schemes`, `Category:- Web Directory`, `Category:- Tourist Places`, `Category:- Catalogues`.

| Taxonomy | What it holds |
|---|---|
| **Services** | Transactional things you do |
| **Schemes** | Benefit programmes you might qualify for |
| **Web Directory** | Links to the same portals, again |
| **Open Data / Catalogues** | Datasets from data.gov.in |

A ration card appears as a *Scheme* (One Nation One Ration Card), a *Service* (state application portals), **and** a *Web Directory* entry ("Ration Card Management System, Assam"). Three doors, same room, no cross-links between them.

To a citizen, "scheme" vs "service" is not a meaningful distinction. They have a **problem**, not a taxonomy.

---

## 6. Smaller but real issues

- **Redundant navigation.** Category pages render the same 18-item list *twice* on one screen — a clipped horizontal strip up top (overflowing to a `⋮` menu) and a full sidebar on the left.
- **Auto-rotating carousel as primary navigation.** The subcategory "Explore" card cycles every ~4 seconds and the CTA's destination changes with it. (I verified label and `href` stay in sync — but a user reaching for a button that retargets mid-reach will still mis-click.)
- **Language switching leaves the domain.** All 13 languages live on *entirely separate punycode domains* (`xn--i1bj3fqcyde.xn--11b7cb3a6a.xn--h2brj9c` for Hindi, etc.), not paths. Breaks session, bookmarks, and any shared link.
- **404s are titled "Home."** Bad URLs return `<title>Home | National Portal of India</title>`. Poisons browser history, tab labels, screen-reader announcements, and SEO.
- **Content is client-rendered only.** Fetching a subcategory page returns HTML with no item data. No-JS users get nothing; crawlers likely see an empty shell.
- **Bureaucratic self-description everywhere.** Sections open by describing themselves rather than routing you: *"This section functions as a comprehensive resource centre aimed at streamlining access to crucial information regarding India's political and leadership systems."* That's 24 words that help nobody.
- **CAPTCHA on a feedback form** — friction on the one channel that reports the site's own failures.

---

## 7. Credit where due — don't throw these away

An honest audit has to say what works:

- **The 18-category taxonomy** is genuinely user-centred. Keep it.
- **The scheme eligibility wizard** (`/my-government/schemes`) is excellent — 6 steps, gender + age + state, returns schemes you actually qualify for. Note it's **"Powered by myScheme"**, i.e. an embed from a *different* portal.
- **The A–Z "Important Services" page** is the most usable thing on the site.
- **Accessibility toolbar** — contrast adjustment, text sizing, screen-reader support. Present and functional.
- **13 language versions** — right instinct, wrong plumbing.
- **Breadcrumbs** are present and correct throughout.
- **Performance is fine.** ~867 KB, 141 requests, DOMContentLoaded ~495 ms. Not the bottleneck; don't waste your project's pitch on it.

---

## 8. What this means for the rebuild

The strategic insight to build your portfolio project around:

> **The scheme wizard already proves the correct model exists inside this site.** Tell it who you are → get what applies to you. That pattern is applied to 5,456 schemes and withheld from 13,819 services. Extending it is the whole thesis.

Four moves, in order of impact:

**1. Make the leaf page the product.**
Replace *"For More Information Visit"* with what a citizen actually needs: eligibility, documents required, fees, processing time, offline alternative, numbered steps, and *then* the apply button. This one change is worth more than any navigation redesign.

**2. Add state as a first-class dimension.**
Ask once, remember it, filter everything. Most of what citizens need is state-delivered. A national portal that can't tell Kerala from Haryana is a national portal that can't help.

**3. Collapse the four taxonomies into one "life event" spine.**
*Having a baby · Losing a job · Someone died · Buying a vehicle · Starting a business · Going abroad.* Behind each, pull in the relevant schemes, services, forms and documents regardless of which internal silo owns them. Keep the 18 topic categories as a secondary browse path.

**4. Make search intent-aware.**
At minimum: filter by state, ministry, central/state, and online/offline; strip stop-words; and never return a tourist attraction to someone searching for a certificate.

### Suggested demo journeys for the portfolio

Pick tasks where the current site measurably fails, and show a before/after click count:

| Task | Today | Target |
|---|---|---|
| Replace a lost ration card in Odisha | 447 results, none relevant | 1 answer page |
| Apply for a birth certificate | 3,685 results incl. tourist sites | 1 answer page |
| Apply for a passport | 5 clicks → 1 paragraph → offsite | 1 page, complete |

---

## Appendix: verified reference points

| Item | Value |
|---|---|
| Search URL pattern | `/search?search=<query>` |
| Category URL | `/category/<slug>` |
| Subcategory URL | `/category/<slug>/subcategory/<slug>` |
| Service leaf URL | `/services/details/<slug>` |
| Search facets available | Catalogues, Services, Web Pages & Documents — **only** |
| Subcategory filter | All / Central / State radio (not present in global search) |
| `birth certificate` results | 3,685 |
| `I lost my ration card` results | 447 |
| `Apply for Driving Licence` results | 256 |
| Health & Wellness advertised / browsable | 3,070 / 492 |
| Homepage links | 228 |
| Page weight | ~867 KB, 141 requests, DCL ~495 ms |
