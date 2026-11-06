# Temporal knowledge system

This is my submission for the Interface.ai Applied AI Engineer take-home: a temporal knowledge system over five dated credit-union website snapshots. I built it with AI coding assistance, which I directed, reviewed, and tested myself.

The contract is small. `build()` returns an object with `ingest(snapshot)`, `answer(question, as_of)`, and `conflicts()`. Answers have to be grounded in the snapshot that was current on the requested date, cite their sources, resolve conflicting pages when they disagree, and say they don't know—with `confident=False` and no invented numbers—when the site does not support a figure. Scoring covers accuracy, conflicts, consistency, and speed.

## How I approached it

I started with the assignment PDF and the participant kit rather than jumping into code. The kit's harness and scorer tell you what "current," a trap, a refusal, and a conflict actually mean, so I read `harness/contract.py`, the public eval file shape, and the selftest before designing anything. The five production crawls are large (hundreds of pages each), and the kit is deliberately not something you commit, so I kept snapshots, the harness, eval files, PDFs, and zips outside version control from day one.

The design choices that matter most showed up early: date selection has to be exact, official rate pages have to beat calculators and blogs, APR is not APY, jumbo is not ordinary, and a missing price is not the same as a missing product. I implemented those as rules in standard-library Python rather than calling an LLM at answer time. That kept the system deterministic, fast, and free of API keys.

## Architecture

### Snapshot selection

`answer(as_of)` uses the latest ingested snapshot whose `taken_on` is on or before `as_of`. With no date, that is simply the newest snapshot supplied to `ingest`, not the live web. A question dated before the first snapshot abstains. An effective date printed on a later crawl is not copied backward—a July question still sees the June snapshot even if an August page says a table was effective in July. `fetched_at` is never treated as the website date.

A snapshot that contains the site root (path `/`) is a full crawl. Pages it omits are treated as deleted. Every production snapshot in the kit includes that root, so a page missing from a later production crawl stays missing. A snapshot with only interior pages is a supplement: URLs it fetched replace the previous copy, and URLs it did not fetch stay. The harness selftest's two toy snapshots are supplements under this rule; a green selftest does not mean a missing production page is still current.

### Facts, authority, and qualifiers

On ingest I parse rate tables and short labeled lines into facts with qualifiers kept separate: product, consumer vs business, purchase vs refinance, term, amount tier, model-year range, IRA wrapper (Roth / Traditional / other), and introductory vs post-promo columns. Footnote markers glued to percents (`4.20%1`) are stripped so they do not become extra digits. Payment-example columns are ignored.

Official rate sheets and product pages outrank calculators, refinance landing pages, homebuying guidance, news, and blogs. When two official pages disagree, the later printed effective date wins, then the rate sheet. APR, APY, dividend rate, and nominal interest rate are different measures. Jumbo vs ordinary, business vs consumer, and IRA wrappers are different products. A question that does not ask for jumbo gets the ordinary row when both exist.

The member-facing text states only the winning value and its qualifiers. A rejected stale number can be mentioned in `conflict_note`, which the scorer does not read. Sources cite the supporting page and, when the rate sheet links to one, the product page in that same snapshot.

### Refusals and non-rate questions

If the product is described but the requested price, term, or branch schedule is not, the answer abstains on that field only (`confident=False`). It does not invent a neighboring product's number, and it does not put a new phone number or date into the refusal.

Non-rate questions use narrow extractors: routing numbers, branch-announcement hours, street lines, year-stamped newsroom boilerplate (branch count, Forbes ordinal, Volunteer Time Off by calendar year), and explicit "ended" or "concluded" notices. "Last year" is not silently rewritten as 2023; VTO counts are labeled with the year the page states.

### Conflicts

`conflicts()` looks only at the latest snapshot. It reports a contradiction when an official table and a lower-authority page disagree on the same measure and the same qualifiers, or when two official pages still disagree on the current claim. Resolution is the official or later value.

Each `values` list is one current claim plus the stale figures that compete with that claim directly (same term, purpose, measure, amount tier, and overlapping model years). A 60-month calculator rate for 2017–20 model years is not listed beside the current 2024–27 rate. Pages omitted by the latest full crawl are not competitors. A supplement does not delete pages it never fetched.

These are not conflicts: a value that changed across dates, two tiers, APR versus nominal rate, dividend versus APY, intro versus post-promo on one offer, Roth versus Traditional, or a duplicate table that agrees again in the latest snapshot. An ended program that a guidance table still prices is one availability conflict, resolved to ended.

## Tradeoffs

I stayed on the Python 3.9+ standard library only—no third-party packages, no LLM calls, no API keys, and no network calls from `answer()`. That makes the system deterministic, easy to grade, and fast enough that speed is not the bottleneck. The cost is that the extractors are rule-based. Authority ranking is mostly generic (calculator, promo, `/rates`, news, blog, pdf), with a few path tokens from this site's vocabulary. Several non-rate extractors are shaped more like the public items than a fully generic reader, so a paraphrase that does not hit the same anchor can abstain or miss even when the page states the fact another way. ATV rows still share one term with no model-year or amount bound, so `conflicts()` over-bundles several calculator figures with each official figure at that term. Those are known limitations, not hidden ones; see `NOTES.md` for the detailed extractor review.

## Layout

| Path | Role |
| --- | --- |
| `solution.py` | Harness entry point; exposes `build()` |
| `ksys.py` | Full implementation (stdlib only) |
| `NOTES.md` | Rules, measured scores, safeguards |
| `tests/test_synthetic.py` | Synthetic fixtures; no crawled pages |
| `scripts/guard_staged_paths.py` | Blocks snapshots / eval / harness / PDFs / zips from being staged |
| `.githooks/pre-commit` | Runs the path guard |
| `requirements.txt` | Empty of third-party deps on purpose |

## How to run it

The participant kit (harness, snapshots, and public eval) is deliberately not in this repository. Place that kit locally, then from the kit directory:

```bash
python3 harness/run_eval.py \
  --submission /path/to/this/repo \
  --snapshots /path/to/unpacked-snapshots \
  --eval eval/public_eval.jsonl

python3 harness/run_eval.py \
  --submission /path/to/this/repo \
  --snapshots harness/selftest/snapshots \
  --eval harness/selftest/eval.jsonl \
  --conflicts-gold harness/selftest/conflicts_gold.json

python3 -m unittest tests.test_synthetic
```

Enable the commit hook if you are working in a clone:

```bash
git config core.hooksPath .githooks
python3 scripts/guard_staged_paths.py --self-test
```

## Results

These measurements were taken on CPython 3.12.3 (Linux x86_64) with no extra packages installed. A clean 3.11 grader can still differ in latency. The starter baseline on the same public file was **0.093**.

| Suite | Result |
| --- | --- |
| Public eval (`eval/public_eval.jsonl`) | **final_score 0.969**, qa_score 0.969, 0 stale-trap hits, 0 errors |
| Public latency | p95 about **0.02–0.03s**; ingest about **25s** (24.9s / 26.1s across two runs) |
| Consistency / speed pillars | both 1.0 (so they equal accuracy) |
| Sole public miss | `dup-005` (empty `must_include`), so **0.969 is the public ceiling** |
| Harness selftest | **final_score 1.0**, conflict F1 **1.0**, resolution accuracy 1.0 |
| Synthetic tests | **14/14** pass |
| Private 30-case extra suite (not committed) | **30/30** on the rate, qualifier, refusal, and year checks |

Public eval was run twice on the five full crawls (637, 634, 635, 683, 705 pages). `conflicts()` on the latest full crawl returned 33 direct-competition records; ATV over-bundling is the main known noise in that list. More detail, including category scores and the wording gap on one private Platinum Visa case, lives in `NOTES.md`.

I did not invent numbers for this write-up. Every score above was produced by the untouched kit harness against this code.
