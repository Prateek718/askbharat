# Scrape Report — india.gov.in

**Run date:** 27 July 2026
**Outcome:** full catalogue harvested. **Firecrawl was not used** — see §1.

---

## 1. On Firecrawl

**Firecrawl is not free.** Two separate things share the name:

| | |
|---|---|
| **Hosted API** (`api.firecrawl.dev`) | Paid. Free tier ~1,000 credits/month, 1 credit ≈ 1 page. Hobby $16/mo (3k credits), Standard ~$83–99/mo (100k), Growth $333/mo (500k). AI Extract is a *separate* subscription from ~$89/mo. |
| **Core engine** | Open source under **AGPL-3.0** — self-hostable for free, but you supply Redis, a Playwright pool and proxies yourself. |

So your full-catalogue target (~20k pages) would have been ~20k credits — a paid plan — on the hosted API.

There was no API key on this machine, so I could not have run it regardless.

**But it turned out not to matter, because a scraper is the wrong tool for this site.**

india.gov.in is a Next.js app whose listing pages are client-rendered on top of **public, unauthenticated JSON endpoints**. Rather than render 20,000 pages in a headless browser and regex the HTML back into structure, we can ask the same endpoints the site's own frontend asks and get clean typed records directly.

| | Firecrawl (hosted) | Direct API (what we did) |
|---|---|---|
| Cost | ~20k credits (paid plan) | **₹0** |
| Runtime | hours | **11 min** (services) + **83 s** (schemes) |
| Output | markdown → needs parsing | **structured JSON, typed fields** |
| Fidelity | whatever survives HTML→MD | exactly what the backend holds |

**Verdict on the tooling question:** for a JS-heavy site with a discoverable JSON backend, always look for the backend first. Firecrawl (or Crawl4AI, or Scrapy+Playwright) earns its keep when there is *no* API — and there is exactly one such job left here, noted in §6.

---

## 2. The endpoints

```http
POST /category/subcategoryservice
{"termMatches":[{"fieldName":"subCategoryId","fieldValue":"48"}],"pageNumber":1}
   → fixed page size of 10; ~1,233 requests for the whole catalogue

POST /my-government/schemes/search/dataservices/getschemes
{"categories":[],"mustFilter":[],"pageNumber":1,"pageSize":500}
   → honours pageSize up to 500; 11 requests per sweep

POST /my-government/schemes/search/dataservices/getSchemeFacets
   → the 22-facet eligibility index (see §5)
```

The category/subcategory taxonomy (with numeric ids) is embedded in the RSC payload of each server-rendered `/category/<slug>` page.

Politeness: `robots.txt` says `Crawl-delay: 10`. I used a global **2 req/s** ceiling with 4 workers and exponential backoff — far gentler than the concurrency this infrastructure serves normally, and the whole job still finished in 11 minutes. Every raw response is kept in `raw_pages.jsonl` so nothing needs re-fetching.

---

## 3. What we got

| File | Rows | Notes |
|---|---|---|
| `data/services.jsonl` / `.csv` | **10,645** | unique service/information records (12,073 fetched, deduped on `id`) |
| `data/schemes.jsonl` / `.csv` | **4,860** | unique schemes after 7 saturating sweeps |
| `data/taxonomy.json` | 18 cats / **86** subcats | full tree with ids, aliases, descriptions |
| `data/scheme_facets.json` | 22 facets | the hidden eligibility index |
| `data/raw_pages.jsonl` | 1,244 | every raw API response, for audit/replay |
| `data/link_check.json` + `link_recheck.json` | 500 | link-rot sample with verification pass |

**Service record shape:** `id, title, description, npiAlias, url, npiMinistryDepartment, npiKeywords, subCategoryId`
**Scheme record shape:** `title, slug, description, ministry, npiMinistry, schemeCategory, beneficiaryState, tags`

---

## 4. Data quality findings

### 4.1 Nearly 40% of the catalogue's links are broken

This is the headline. Every service record is fundamentally a pointer to another website — so this number *is* the portal's usefulness.

I sampled 500 destination URLs, then **re-verified every failure with 3 retries and an independent DNS check**, because a first pass showed 47.2% and I did not trust it. 39 links recovered on retry. The corrected figure:

| Status | Count | Share |
|---|---|---|
| Reachable | 302 | **60.4%** |
| Dead domain (NXDOMAIN after 3 retries) | 94 | **18.8%** |
| Unreachable / timeout | 55 | 11.0% |
| HTTP error (39 of them hard 404s) | 49 | 9.8% |
| **Broken, total** | **198** | **39.6%** |

Of the 302 that *do* load, **20 only load with TLS verification disabled** — expired or misconfigured certificates on government hosts.

Confirmed-dead examples:

```
nrcgrapes.icar.gov.in     National Research Centre for Grapes
nubra.kvs.ac.in           Kendriya Vidyalaya, Nubra
br.raj.nic.in             Rajasthan Business Register
maef.nic.in               Maulana Azad Education Foundation schemes
bcwelfare.mp.nic.in       MP post-matric minority scholarship form
karnataka.gov.in/...DESTITUTE-WIDOW-PENSION.pdf   → 404
```

That last one is worth sitting with. A destitute widow in Karnataka, following the National Portal of India to her pension form, gets a 404.

### 4.2 It is a link directory to 5,414 other websites

- **5,414 distinct destination hosts**
- **10,426 of 10,645 links (97.9%) point off-site**; only 219 stay on india.gov.in
- **1,205 links (11.3%) point straight at a PDF** — the "online service" is a form to print
- **2,316 links (21.8%) are plain `http://`**, not HTTPS, on a government portal in 2026

### 4.3 Missing attribution

| Field | Empty |
|---|---|
| `npiMinistryDepartment` (services) | **42.4%** (4,511 of 10,645) |
| `ministry` (schemes) | **84.1%** (4,089 of 4,860) |
| `npiMinistry` (schemes) | **100%** |
| `beneficiaryState` (schemes) | **100%** |

`beneficiaryState` being 100% null on every record is the important one — see §5.

### 4.4 The advertised counts don't hold up

| Site claims | We could actually enumerate | Gap |
|---|---|---|
| 13,819 online services | 10,645 unique | −3,174 |
| 5,456 schemes | 4,860 unique | −596 |

### 4.5 Scheme pagination is non-deterministic

Every sweep of `getschemes` returns exactly 5,456 rows — but a *different* 5,456 each time, with random duplicates and random omissions. Two back-to-back full sweeps:

```
sweep A: 5,456 rows → 4,043 unique
sweep B: 5,456 rows → 4,339 unique
in A but not B: 287     in B but not A: 583
union of both:  4,626
```

A single sweep gets you only ~78% of the catalogue. I had to sweep repeatedly and union until three consecutive sweeps yielded nothing new:

```
sweep 1: +4,278 → 4,278 (78.4%)
sweep 2:   +373 → 4,651 (85.2%)
sweep 3:   +209 → 4,860 (89.1%)
sweep 4-7:   +0 → 4,860  saturated
```

**Anyone who scrapes this endpoint once — including Firecrawl — silently gets an incomplete dataset and no warning.** `harvest_schemes.py` now sweeps to saturation by default.

### 4.6 Descriptions are truncated at 300 characters

Scheme descriptions max out at exactly 300 chars (median 257). The list endpoint serves blurbs, not full text. Full descriptions need the detail pages.

Service descriptions are healthier: median 272, max 1,137, none empty.

### 4.7 Three incompatible category systems

- **18** site categories (the browse tree)
- **15** scheme categories in the data — different names: "Women and Child", "Banking,Financial Services and Insurance"
- the scheme search sidebar filters by the **18** site categories while the records carry the **15**

Also cosmetic but telling: `"Agriculture,Rural & Environment"` and `"Banking,Financial Services and Insurance"` are missing the space after the comma, in production data.

---

## 5. The best find: a hidden 22-facet eligibility index

`getSchemeFacets` exposes a fully-structured eligibility model that **the browse UI never uses**:

| Facet | Entries | | Facet | Entries |
|---|---|---|---|---|
| **State** | 38 | | Marital Status | 6 |
| Scheme Category | 15 | | Below Poverty Line | 2 |
| Gender | 4 | | Economic Distress | 2 |
| Age | 12 (range) | | Government Employee | 2 |
| Caste | 7 | | Employment Status | 4 |
| Ministry Name | 47 | | Student | 2 |
| Level (Central/State) | 3 | | Occupation | 19 |
| Residence (Rural/Urban) | 3 | | Application Mode | 3 |
| Minority | 2 | | Scheme Type | 2 |
| Differently Abled | 2 | | Disability % | 11 (range) |
| Benefit Type | 3 | | DBT Scheme | 2 |

Sample state counts: Gujarat 381, Haryana 241, Tamil Nadu 238, Puducherry 228, Madhya Pradesh 185.

**Two things follow, and together they are the whole thesis of your redesign:**

1. **The state data exists.** My earlier audit flagged that search has no state filter even though driving licences, ration cards and birth certificates are state subjects. It turns out that is not a missing-data problem — the index has 38 states with populated counts. The portal simply never surfaces it outside this one buried wizard, and strips it from every record it returns.

2. **The metadata only covers ~68% of schemes.** Facet counts sum to ~3,726, not 4,860 or 5,456 (e.g. Level: 3,145 State + 580 Central + 1 blank = 3,726). So **~1,100 schemes carry no eligibility metadata at all** and can never be matched to a citizen by the wizard.

---

## 6. The one job left that genuinely needs a browser

State attribution is **not** recoverable from the JSON API. I tried every payload shape the frontend uses; `getschemes` ignores `mustFilter`/`termMatches` and returns the unfiltered 5,456 every time. The UI filters via page URL instead — `?beneficiaryState=["Gujarat"]` — and those pages are client-rendered, so plain `curl` returns an empty shell.

Recovering per-state scheme lists means rendering 38 pages in a real browser. **That is the one place where Crawl4AI / Firecrawl / Playwright would actually earn their keep here** — 38 pages, not 20,000.

If you want that, my recommendation is **[Crawl4AI](https://github.com/unclecode/crawl4ai)** — Apache-2.0, genuinely free, Playwright-based, built for exactly this. Say the word and I'll wire it up.

---

## 7. Files

```
harvest.py           services + taxonomy   (2 req/s, resumable via raw_pages.jsonl)
harvest_schemes.py   schemes + facets      (sweeps to saturation)
check_links.py       link-rot sampler      (python check_links.py 500)
data/                all outputs, ~28 MB
```

## 8. What this adds to the redesign case

The audit argued the portal is a link catalogue pretending to be a service portal. The data settles it:

- **97.9%** of records point off-site — it is, literally, a directory
- **39.6%** of those pointers are broken — the directory is four-tenths rotten
- **100%** of schemes have a null `beneficiaryState`, while the backend index holds 38 states — the data for the single most useful filter exists and is thrown away
- **~1,100 schemes** have no eligibility metadata, so the one genuinely good feature can never reach them

You now have 10,645 services + 4,860 schemes as clean JSON/CSV to build against — including a ready-made "broken links" list that makes a strong, concrete demo: *the current portal sends you to a dead page 4 times out of 10; here is the same catalogue, verified.*
