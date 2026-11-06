# How this knowledge system works

No API keys. No third-party packages. The code is standard-library Python 3.9+ (`from __future__ import annotations`). `solution.py` exposes `build()`. The object implements `ingest`, `answer`, and `conflicts`.

These measurements were not run on Python 3.11. The interpreter was CPython 3.12.3 (GCC 13.3.0) on Linux 6.12.113+ x86_64, glibc 2.39, with `PYTHONDONTWRITEBYTECODE=1` and no extra packages installed. A clean 3.11 grader can still differ in latency.

## What "current" means

`answer(as_of)` uses the latest snapshot with `taken_on <= as_of`. With no date, that is the newest snapshot supplied to `ingest`, not the live web. A question dated before the first snapshot abstains. An effective date printed on a later crawl is not copied backward: a July question sees the June snapshot even if an August page says a table was effective in July. `fetched_at` is not used as the website date.

A snapshot that contains the site root (path `/`) is a full crawl. Pages it omits are deleted. Every supplied production snapshot includes that root, so a page missing from a later production crawl stays missing. A snapshot with only interior pages is a supplement: URLs it fetched replace the previous copy, and URLs it did not fetch stay. The harness selftest's two snapshots are supplements under this rule. They are not treated as full crawls. A green selftest does not mean a missing production page is still current.

## How an answer is chosen

Ingest parses rate tables and short labeled lines into facts with qualifiers: product, consumer vs business, purchase vs refinance, term, amount tier, model-year range, IRA wrapper (Roth / Traditional / other), and introductory vs post-promo columns. Footnote markers glued to percents (`4.20%1`) are stripped so they do not become extra digits. Payment-example columns are ignored.

Official rate sheets and product pages outrank calculators, refinance landing pages, homebuying guidance, news, and blogs. When two official pages disagree, the later printed effective date wins, then the rate sheet. APR, APY, dividend rate, and nominal interest rate are different measures. Jumbo vs ordinary, business vs consumer, and IRA wrappers are different products. A question that does not ask for jumbo gets the ordinary row when both exist.

The member-facing text states only the winning value and its qualifiers. A rejected stale number can be mentioned in `conflict_note`, which the scorer does not read. Sources cite the supporting page and, when the rate sheet links to one, the product page in that same snapshot.

If the product is described but the requested price, term, or branch schedule is not, the answer abstains on that field only (`confident=False`) and does not invent a neighboring product's number or put a new phone number or date in the refusal.

Non-rate questions use narrow extractors: routing numbers, branch-announcement hours, street lines, year-stamped newsroom boilerplate (branch count, Forbes ordinal, Volunteer Time Off by calendar year), and explicit "ended" or "concluded" notices. "Last year" is not silently rewritten as 2023; VTO counts are labeled with the year the page states.

## Conflicts

`conflicts()` looks only at the latest snapshot. It reports a contradiction when an official table and a lower-authority page (calculator, promo, stale guidance) disagree on the same measure and the same qualifiers, or when two official pages still disagree on the current claim. Resolution is the official or later value.

Each `values` list is one current claim plus the stale figures that compete with that claim directly (same term, purpose, measure, amount tier, and overlapping model years). A 60-month calculator rate for 2017–20 model years is not listed beside the current 2024–27 rate. A strictly older effective date is not a second current claim when the latest effective date is unanimous. Pages omitted by the latest full crawl are not competitors. A supplement does not delete pages it never fetched.

These are not conflicts: a value that changed across dates, two tiers, APR versus nominal rate, dividend versus APY, intro versus post-promo on one offer, Roth versus Traditional, or a duplicate table that agrees again in the latest snapshot. An ended program that a guidance table still prices is one availability conflict, resolved to ended.

## Check locally

The participant kit stays outside this repository. A reviewer with that kit can verify the commit from the kit directory:

```bash
python3 harness/run_eval.py --submission /path/to/this/repo --snapshots /path/to/snapshots --eval eval/public_eval.jsonl
python3 harness/run_eval.py --submission /path/to/this/repo --snapshots harness/selftest/snapshots --eval harness/selftest/eval.jsonl --conflicts-gold harness/selftest/conflicts_gold.json
```

Synthetic tests, with no crawled pages:

```bash
python3 -m unittest tests.test_synthetic
```

The selftest lost-card page is only in the first toy snapshot. That snapshot has no site root, so the later two-page pull is a supplement and the fee stays. The same fee is dropped when both snapshots include `/`, which the synthetic tests lock. The selftest conflict (2.95% ended promo vs 3.50% rate sheet) is found and resolved to 3.50%.

On the tree before the supplement rule, the untouched selftest scored qa_score 0.80 and final_score 0.83 because that lost-card item abstained. After the rule, the same untouched command scores qa_score 1.0 and final_score 1.0, with conflict F1 1.0 and resolution accuracy 1.0. Public `answer()` text did not change.

## Keeping event data out of git

Snapshots, eval files, the harness, PDFs, zips, and QA extracts are gitignored. A repository hook refuses to stage them:

```bash
git config core.hooksPath .githooks
python3 scripts/guard_staged_paths.py --self-test
```

`git diff --cached --name-only | python3 scripts/guard_staged_paths.py --stdin` checks whatever is already staged.

## Measured on this machine

Commands, from the kit directory, with snapshots and results kept outside this repo. Python 3.12.3, not 3.11. No judge.

```bash
python3 harness/run_eval.py --submission /path/to/this/repo --snapshots /path/to/unpacked-snapshots --eval eval/public_eval.jsonl
python3 harness/run_eval.py --submission /path/to/this/repo --snapshots harness/selftest/snapshots --eval harness/selftest/eval.jsonl --conflicts-gold harness/selftest/conflicts_gold.json
python3 -m unittest tests.test_synthetic -q
```

Public eval was run twice on the five full crawls (637, 634, 635, 683, 705 pages). Before and after the supplement rule, `answer()` text was identical on all 40 items.

- final_score **0.969**, qa_score **0.969**, stale-trap hits 0, errors 0
- weighted points 62.5 / 64.5; the only item under 1.0 is `dup-005` (weight 2, score 0) because `must_include` is empty
- consistency 1.0 and speed 1.0, so those pillars equal accuracy
- first run: p95 0.03s, max 0.19s, ingest 24.9s
- second run: p95 0.02s, max 0.17s, ingest 26.1s
- both speed scores are 1.0; the latency difference is measurement noise inside the full-speed band
- category scores stayed 1.00 except `list_completeness` 0.75

Selftest after the supplement rule: qa_score **1.0**, final_score **1.0**, conflicts submitted 1, F1 **1.0**, resolution accuracy **1.0**, ingest 0.0s. Before that rule, on this same machine: qa_score **0.80**, final_score **0.83**, with `current_value` at 0.

`python3 -m unittest tests.test_synthetic -q`: 14 tests, OK.

A private 30-case check against the real snapshots, not committed, passed the rate, qualifier, refusal, and year checks (30/30 on those assertions). One wording gap: the Platinum Visa transfer answer includes 2.90%, 90 days, and six months, and it does not include the 19.50% post-intro upper bound named in that case's expected behavior. `conflicts()` on the latest full crawl returned **33** records, identical before and after the supplement rule. Each production snapshot was classified as a full crawl (`page_urls` were not padded from earlier snapshots). The 33 records are direct-competition listings: auto calculator versus the overlapping model-year claim, mortgage guidance versus the matching tier and measure, checking landing pages versus the rate sheet, ATV terms, and Mortgage Rate Relief resolved to ended. ATV rows still share one term with no model year or amount bound, so several calculator figures are listed with each official figure at that term. The record lines stay out of git.

## Extractor review

This is a reading of `ksys.py`, not a score. Public 0.969 does not show that hidden paraphrases will hit the same anchors.

Authority is mostly generic (calculator, promo, `/rates`, news, blog, pdf). A few path tokens are this site's vocabulary: `switch-and-earn`, `homebuying-guidance`, `earn-more`, `offer-lp`, and an `info.` host. They change which page wins; they are not stored question-to-answer pairs.

Several non-rate extractors are shaped like the public items rather than a generic reader. The Hy-Vee closure scan keeps lines that say March 31. Relocation lines are kept only when they contain both Key West and Sycamore and not CO-OP. The card-note clip starts at the literal `2.90`. Volunteer Time Off parsing requires the institution's name in the sentence. Drive-up hours fall back to lines that contain 8 a.m. and 6 p.m. Eligibility looks for Iowa, Illinois, and Wisconsin together. Home$tart looks for `15,000`. Student-loan prose skips share-savings sentences. Contact updates look for Shine and an 800 number. Those paths were not rewritten here. `answer()` text on the public file was unchanged, so this pass did not retune them. A question that does not hit the anchor can abstain or miss even when the page states the fact another way.

No answer call opens a network connection. The only subprocess is the path guard's `git diff --cached`. `KSYS_TRACE`, if set, prints the parsed question features locally. Snapshots, eval files, and the proposed-case file are not in this repository.
