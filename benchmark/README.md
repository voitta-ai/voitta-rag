# Code-context benchmark harness

Decision benchmark for the context layer: which strategy gets the right bytes into
the model's window, at what token cost, without damaging answer quality.

Methodology lives in [voitta-ai/voitta-rag#43](https://github.com/voitta-ai/voitta-rag/issues/43)
(first comment) and the plan doc it came from. This directory is the executable
half. The tracking issues are
[hq#75](https://github.com/method-and-apparatus/hq/issues/75),
[#87](https://github.com/method-and-apparatus/hq/issues/87),
[#88](https://github.com/method-and-apparatus/hq/issues/88),
[#108](https://github.com/method-and-apparatus/hq/issues/108), and
[voitta-rag#49](https://github.com/voitta-ai/voitta-rag/issues/49).

## Why it started small

The full matrix in the plan is 30 questions x 5 modes = 150 judged runs, gated
behind an interview-format question-drafting session. It was specified on
2026-05-17 and sat unrun for ten weeks. v0 shipped the smallest cut that produces
real numbers -- 1 repo, 5 questions, 3 modes -- and every axis dropped then has
since been added as configuration plus one builder:

| Axis | now | How to expand further |
|---|---|---|
| Repos | jsoup (unfamiliar) | second `config.json`, or parameterise `repo` |
| Questions | 5, one per class | append to `questions.jsonl` |
| Modes | 10 (see `BUILDERS` / `AGENTIC_MODES`) | add a builder + an entry |
| Chains | tldr -> rag, tldr -> cce | a builder that calls two others in sequence |
| Output-side | `--caveman-output` | orthogonal to mode; any mode can run in both styles |

Adding a context builder still requires no change to `runner.py` or `judge.py`.
The one exception is `AGENTIC_MODES`: `cce` answers by giving the model read-only
repo tools rather than an injected context string, which is a different
interaction shape, so `runner.py` dispatches it separately.

## Layout

| File | Role |
|---|---|
| `config.json` | repo, models, effort, pricing, mode list, per-mode settings |
| `questions.jsonl` | the locked question set (one per question class) |
| `modes.py` | one context builder per mode; the only mode-aware code |
| `runner.py` | runs every (question, mode) pair, writes one JSONL record each |
| `judge.py` | scores answers with read-only tools over the repo under test |
| `report.py` | aggregates scored runs into mode x question-class tables |

`runner.py` imports `judge.py` for its `read_file` / `grep` / `glob` tools, which
the `cce` mode reuses so the exploring model and the verifying judge see the
repository through exactly the same interface.

## Setup

```sh
python3 -m venv .venv
. .venv/bin/activate
pip3 install -r requirements.txt
export ANTHROPIC_API_KEY=...   # or: ant auth login
```

Per-mode prerequisites:

- `baseline` -- none.
- `llm-tldr` -- the `tldr` CLI on `PATH` (`pip3 install llm-tldr`).
- `voitta-rag` -- a running instance reachable at `voitta_rag_mcp_url`, with the
  repo under test indexed **and its folder Search toggle active**. A folder that
  is indexed but inactive returns nothing, which scores as a total retrieval
  failure rather than a configuration error. Verify before running:
  `list_indexed_folders` / `set_folder_active`.

  Mount the repo into the container (`docker-compose.override.yml`), then register and
  index it. The REST API authenticates by cookie, not by token -- an unauthenticated call
  returns `{"detail":"Temporary Redirect"}` (a 307 to `/browse`), which reads like a URL
  problem and is not one:

  ```sh
  curl -s -b "voitta_user_id=1" -X PUT localhost:58000/api/settings/folders/jsoup \
    -H 'Content-Type: application/json' -d '{"enabled": true}'
  curl -s -b "voitta_user_id=1" -X PUT localhost:58000/api/settings/folders/jsoup/search-active \
    -H 'Content-Type: application/json' -d '{"search_active": true}'
  ```

  `enabled: true` is what queues the folder for indexing; `search-active` is a separate
  toggle and both are required. Poll `list_indexed_folders` over MCP for progress --
  `GET /api/settings/folders/{path}` returns the toggles only, never the index status.

  **`include_folders` does not scope to a subtree.** Over MCP it is an *exact* match on a
  chunk's `folder_path`, and `folder_path` is the directory the file sits in, not the index
  root. `mcp_server.search` does contain subtree-prefix expansion, but only on the
  `if user_name:` branch, and the MCP tool signature has no `user_name` parameter -- so over
  MCP that expansion never runs. Passing `["jsoup"]` therefore scopes to files sitting
  *directly at* the repo root: `CHANGES.md`, `change-archive.txt`, `README.md`, `LICENSE`,
  `SECURITY.md`. Nothing under `src/`. It returns plausible-looking hits, so it fails
  silently rather than erroring. `_expand_index_folders` in `modes.py` enumerates the
  directories locally and passes all of them; `voitta_rag_index` / `voitta_rag_java_index`
  configure it.

  Scoping is still required -- left unset, search runs across every indexed folder on the
  instance, which on a dogfooding instance includes this repository, so the RAG arm would be
  retrieving over its own source.

Check out the repo under test and point `config.json` at it:

```sh
git clone https://github.com/jhy/jsoup ~/g/git/jsoup
```

## Run

```sh
python3 runner.py --config config.json --questions questions.jsonl
python3 judge.py  --runs results/runs-<stamp>.jsonl
python3 report.py --scored results/runs-<stamp>-scored.jsonl
```

`runner.py --modes cce` re-runs a single mode; `--caveman-output` applies the
output-side overlay. `report.py --scored` takes several files, so arms run at
different times report together. Both scripts append, so a failed mode can be
re-run into the same file without discarding good records.

**Quote mode names containing `>`.** The chain modes were originally called
`llm-tldr->voitta-rag`; unquoted on a shell command line the `>` is a redirect,
so `--modes llm-tldr->cce` silently ran a mode named `llm-tldr-` and truncated a
file called `cce` in the working directory. They are now `llm-tldr-then-cce` and
`llm-tldr-then-voitta-rag`.

## Method notes

**Identical prompt across modes.** `PROMPT_TEMPLATE` in `runner.py` is the same
for every mode; only the injected context differs. Any measured difference is
attributable to the context strategy, not to prompt variation.

**Citations are the correctness lever.** The answering prompt requires a
`file:line` citation on every factual claim, and the judge has `read_file`,
`grep`, and `glob` over the repo, so it resolves each citation against real source
instead of scoring plausibility. `bogus_citation_count` is the metric that catches
a strategy which returns confident, well-formed, wrong answers.

**Judge scores coverage, not importance.** The judge prompt asks for every problem
found, including uncertain and minor ones. A "report only significant issues"
instruction gets followed literally by current models and depresses measured
recall even when the underlying analysis improved -- filter downstream instead.

**Token counts come from the API.** `tokens_in` / `tokens_out` are read from the
`usage` field on each response. The `baseline_char_budget` is a packing heuristic
for assembling the dump, never a token estimate.

**Self-reference.** voitta-rag and llm-tldr are Python; the repo under test is
Java. No tool is retrieving over its own source.

## Results — 2026-08-08

jsoup @ `d24b16d9`, 97 `.java` files (tests and `target/` excluded). Answers on
Claude Sonnet 5 at effort `high`; judge Claude Opus 5 at effort `high` with
read-only repo tools resolving every citation. Five questions, one per class.
Raw records in `results/`.

`+caveman-out` rows are the same tokens-in mode re-run with hq#88's output-side
compression overlay; it is orthogonal to the mode, so it appears as its own row
rather than being averaged in.

| mode | n | score /12 | tokens in | tokens out | sec | $/q | verified | bogus |
|---|---|---|---|---|---|---|---|---|
| **cce** | 5 | **11.40** | 288,342 | 6,200 | 86.5 | 0.6387 | 142 | 2 |
| llm-tldr-then-cce | 5 | 11.00 | 288,436 | 6,097 | 79.2 | 0.6378 | 124 | **0** |
| baseline | 5 | 10.80 | 404,878 | 4,547 | 44.9 | 0.8552 | 65 | 11 |
| baseline +caveman-out | 5 | 10.60 | 404,957 | 14,859 | 126.3 | 0.9585 | 97 | 2 |
| repomix | 5 | 10.40 | 406,277 | 12,486 | 105.8 | 0.9374 | 69 | 17 |
| caveman-compression | 5 | 10.40 | 363,613 | 5,048 | 53.7 | 0.7777 | 86 | 1 |
| cce +caveman-out | 5 | 10.40 | 230,064 | 4,916 | 70.9 | 0.5093 | 129 | 2 |
| llm-tldr-structural | 5 | 6.60 | 66,895 | 1,824 | 18.5 | 0.1520 | 83 | 1 |
| voitta-rag | 5 | 5.20 | 4,337 | 2,024 | 20.5 | 0.0289 | 26 | 24 |
| voitta-rag-java | 5 | 4.80 | 4,334 | 2,314 | 22.9 | 0.0318 | 30 | 29 |
| voitta-rag-java +caveman-out | 5 | 4.60 | 4,413 | 2,483 | 27.1 | 0.0337 | 8 | 41 |
| llm-tldr-then-voitta-rag | 5 | 4.20 | 9,305 | 2,552 | 26.0 | 0.0441 | 36 | 15 |
| llm-tldr +caveman-out | 5 | 3.40 | 5,258 | 1,063 | 12.1 | 0.0212 | 1 | 33 |
| llm-tldr | 5 | 2.40 | 5,179 | 1,325 | 13.6 | 0.0236 | 2 | 29 |

Per question class (mean /12), tokens-in modes only:

| class | cce | baseline | repomix | caveman-compr | tldr-then-cce | tldr-struct | voitta-rag | voitta-rag-java | tldr-then-rag | llm-tldr |
|---|---|---|---|---|---|---|---|---|---|---|
| architecture | 12 | 11 | 12 | 10 | 11 | 7 | 7 | 7 | 6 | 2 |
| change-planning | 11 | 7 | 9 | 10 | 10 | 9 | 9 | 6 | 7 | 3 |
| edge-case-dependency | 11 | 11 | 11 | 11 | 11 | 7 | 3 | 5 | 3 | 1 |
| implementation-lookup | 12 | 9.5 | 9 | 11 | 11 | 7 | 6 | 5 | 4 | 5 |
| path-tracing | 11 | 8.5 | 11 | 10 | 12 | 3 | 1 | 1 | 1 | 1 |

### The headline: don't fill the window, let the model go get it

**Agentic exploration beats every packing strategy, and costs less than the full
dump.** `cce` -- the model with `read_file` / `grep` / `glob` and no injected
context -- scores **11.40/12 on 288K cumulative tokens at $0.64/question**, against
the full dump's **10.80 on 405K at $0.86**. Better answers, 29% fewer tokens, 25%
cheaper. It also produced **142 verified citations against 2 bogus**, the best
citation record in the benchmark, because it read the lines it cited instead of
being handed a summary of them.

That is the finding the whole exercise exists to produce, and it inverts the
premise the tools are sold on. Every tokens-in tool here is trying to answer "how
do I fit the codebase into the window." On this question set the better move is
not to fit it at all.

The caveat that keeps this honest: `tokens_in` for an agentic mode is **cumulative
across the tool loop**, not one request. It is the right number for cost, and it is
not comparable to a one-shot mode's single-request figure without saying so.

### llm-tldr: the first result was the adapter, not the tool

The 2026-07-27 run reported llm-tldr at 2.40/12 with 29 fabricated citations, and
flagged that it measured one adapter. That flag was worth keeping:

| adapter | score | verified | bogus | tokens in | $/q |
|---|---|---|---|---|---|
| `semantic search --expand` | 2.40 | 2 | 29 | 5,179 | 0.0236 |
| `semantic search` -> `extract` | **6.60** | **83** | **1** | 66,895 | 0.1520 |

Swapping the subcommand nearly triples the score and takes bogus citations from 29
to 1. The mechanism is exactly as predicted: `semantic search` reports `"line": 1`
for every code unit, while `extract` carries real `line_number` fields. The tool
was never the problem; the adapter was throwing away the line numbers before the
model ever saw them.

**`llm-tldr-structural` is also the best quality-per-token in the benchmark** --
6.60 at 67K tokens and $0.15/question, roughly a sixth of baseline's cost for 61%
of its score. If you are optimising cost-per-point rather than peak quality, it is
the pick.

(`tldr structure` was the other candidate adapter and is unusable here: no line
numbers at all, and it parses 50 of the 97 files.)

### Retrieval underperforms here, and the corpus was not the reason

The previous writeup blamed voitta-rag's score on corpus asymmetry -- it indexed
233 files including changelogs while the other arms saw 97 `.java` files. That
hypothesis is now tested directly and **it was wrong**:

| | score | verified | bogus |
|---|---|---|---|
| `voitta-rag` (233 files, whole checkout) | 5.20 | 26 | 24 |
| `voitta-rag-java` (97 files, exactly the benchmark corpus) | 4.80 | 30 | 29 |

Matching the corpus did not help; it scored marginally *lower*. Both arms carry
roughly as many bogus citations as verified ones. The mechanism is visible in the
chunk record: it has `chunk_index` but **no line numbers**, so a model given a
correct chunk still cannot cite `file:line` and reconstructs one. This is the same
failure as the first llm-tldr adapter, from the same cause, and it is the highest-
value fix for this arm.

Chaining does not rescue it either: `llm-tldr-then-voitta-rag` scores 4.20, below
both of its halves. Chaining onto the agentic loop is the one that works --
`llm-tldr-then-cce` at 11.00 with **zero bogus citations across all five
questions** -- though it does not beat plain `cce`, so the seed earns nothing here.

### Repomix is the same dump with a nicer cover page

Pointed at the same include/exclude globs, Repomix emits **1,127,414 characters
against the plain dump's 1,119,819** and scores 10.40 against 10.80. Its
advertised ~70% token reduction is *file selection* -- honouring `.gitignore`,
dropping binaries -- not compression of the files it keeps. Once your globs are
already scoped, there is nothing left for it to select, and what remains is
formatting. It is a good packer; it is not a compressor, and hq#88 listed it under
a claim it does not make on this workload.

### Two axes, and one measurement trap

hq#88's real contribution is separating tokens-in from tokens-out. Both halves
produced a result.

**Input side.** `caveman-compression` (spaCy, rule-based) cut the dump 1,119,819 ->
1,014,004 chars, **9.4%**, and cost 0.4 points (10.40 vs 10.80) -- roughly neutral,
and better than expected given what it does to source: `Map. Entry < String String >`
is what survives of `Map.Entry<String,String>`. It strips the punctuation that
makes code parseable and the model reconstructs it anyway. Note the tool is built
for prose; this is a test of a mismatch, not of the tool used as intended. The
LLM-backed variant was deliberately not used -- a non-deterministic compressor
inside a cell makes the cell unattributable.

**Output side.** The overlay reliably shrinks the visible answer at little quality
cost:

| mode | answer chars | with overlay | score | with overlay |
|---|---|---|---|---|
| baseline | 5,699 | 4,496 (-21%) | 10.80 | 10.60 |
| cce | 6,389 | 4,029 (-37%) | 11.40 | 10.40 |
| voitta-rag-java | 4,665 | 3,659 (-22%) | 4.80 | 4.60 |
| llm-tldr | 2,387 | 1,862 (-22%) | 2.40 | 3.40 |

**But you cannot measure the output axis with `tokens_out` while adaptive thinking
is on.** `tokens_out` bills thinking and visible text together, and thinking
dominates and varies wildly: `baseline +caveman-out` shrank its visible answer 21%
while its `tokens_out` went *up* 3x, from 4,547 to 14,859. Anyone benchmarking
output-side compression against `tokens_out` on a thinking model will measure
noise. Measure the rendered answer.

### Cost

**$30.95 answering + $51.90 judging = $82.85 across 75 scored cells.**

Judging costs more than answering. That is not overhead -- verification is a
tool-using agent reading real source, and it is the only reason any of the
citation findings above exist. hq#87 relayed a "<$20 for a controlled eval"
target; a controlled eval of 14 mode-variants at this rigour is roughly 4x that.
The cheap version of this benchmark is the one that reports token ratios and gets
llm-tldr backwards.

## Known limits

- **Five questions.** Enough to catch a large effect, not enough to rank close
  results. Treat the 10.40-10.80 cluster (baseline / repomix / caveman-compression
  / cce+caveman-out) as unresolved, not as an ordering. The gaps that are safe to
  read are the large ones: cce over the compressed modes, and both llm-tldr
  adapters against each other.
- **One repo, and an unfamiliar one.** The familiar-repo arm in the plan, which is
  where structural indexes should do best, is not measured.
- **Single judge, single pass, no inter-rater check.**
- **Agentic `tokens_in` is cumulative**; one-shot modes report a single request.
- **`caveman-compression` is prose tooling on source code** by hq#88's design, not
  the tool used as intended.
- **hq#108 (graphify) is not covered.** It needs a relational / multi-hop /
  map-the-subsystems question class first -- the current five have no cell where an
  inferred graph should win.

## Harness bugs found by running this

Four, all of which produced plausible wrong numbers rather than errors. Kept here
because the debugging is the reusable part.

1. **`max_tokens` caps thinking plus text.** At 16000, a 226K-token context spent
   the entire budget inside thinking and emitted zero text blocks -- billed in
   full, `stop_reason: max_tokens`, empty answer. Raised to 32000; the 406K-token
   repomix cells then hit the same wall, so it is now 64000. Symptom is an empty
   `raw_output` with a full bill.
2. **The SDK refuses long non-streaming requests.** Raising `max_tokens` tripped
   `ValueError: Streaming is required for operations that may take longer than 10
   minutes`, failing every cell identically. `answer_one` streams and takes
   `usage` off `get_final_message()`.
3. **`baseline` was a size-biased subset, not a full dump.** The packer skipped
   any file that overflowed the budget and continued down the list -- a size filter
   wearing a budget's clothing. It admitted 69 of 97 files and dropped the largest:
   `Parser.java`, `Tokeniser.java`, `TreeBuilder.java`, `HtmlTreeBuilder.java`.
   The architecture question asks about exactly those classes, and the answer
   truthfully reported them "absent from the provided files". Fixing it moved
   baseline **6.60 -> 10.80**, so every cross-mode comparison in the first writeup
   was anchored to a control that was wrong by 4.2 points. It now stops at the
   budget and says so in the header.
4. **`include_folders` is not subtree scoping.** Over MCP it is an *exact* match on
   a chunk's parent directory. Passing `["jsoup"]` scoped retrieval to the five
   files at the repo root -- `CHANGES.md`, `change-archive.txt`, `README.md` -- and
   excluded all of `src/`. Retrieval returned real, well-formed hits; the model
   correctly said the source was not there; and the 2026-07-31 writeup recorded
   "retrieval ranks changelogs above source" as a finding about RAG. It was a
   finding about the filter. `_expand_index_folders` now enumerates directories
   locally. **Silent scope failures do not error, they produce publishable
   conclusions** -- print what an arm actually retrieved before theorising about
   why it lost.
