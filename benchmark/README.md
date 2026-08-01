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

## Why v0 is small

The full matrix in the plan is 30 questions x 5 modes = 150 judged runs, gated
behind an interview-format question-drafting session. It has been specified since
2026-05-17 and never run. This harness ships the smallest cut that produces real
numbers -- **1 repo, 5 questions, 3 modes = 15 runs** -- with every dropped axis
expressible as configuration rather than new code:

| Axis | v0 | How to expand |
|---|---|---|
| Repos | jsoup (unfamiliar) | second `config.json`, or parameterise `repo` |
| Questions | 5, one per class | append to `questions.jsonl` |
| Modes | baseline, llm-tldr, voitta-rag | add a builder to `modes.py` + an entry in `BUILDERS` |
| Chains (tldr -> rag) | not in v0 | a builder that calls two others in sequence |
| Output-side (caveman) | not in v0 | a second answering pass; orthogonal axis, see hq#88 |

Expanding an axis should never require touching `runner.py` or `judge.py`.

## Layout

| File | Role |
|---|---|
| `config.json` | repo, models, effort, pricing, mode list, per-mode settings |
| `questions.jsonl` | the locked question set (one per question class) |
| `modes.py` | one context builder per mode; the only mode-aware code |
| `runner.py` | runs every (question, mode) pair, writes one JSONL record each |
| `judge.py` | scores answers with read-only tools over the repo under test |
| `report.py` | aggregates a scored run into mode x question-class tables |

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

  Set `voitta_rag_include_folders` to the repo under test. Left `null`, search runs across
  every indexed folder on the instance, which on a dogfooding instance includes this
  repository -- the RAG arm would be retrieving over its own source.

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

`runner.py --modes voitta-rag` re-runs a single mode. Both scripts append, so a
failed mode can be re-run into the same file without discarding good records.

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

## Second run — 2026-07-31 (voitta-rag arm + baseline q4 re-run)

`results/runs-20260731T222455Z-scored.jsonl`, combined with the first run's records for
the two modes it covered. jsoup @ `d24b16d9`, same models and judge as below.
`voitta_rag_include_folders` is set to `["jsoup"]` so retrieval cannot reach the other
indexed corpora (which include this repository — without the scope, the RAG arm would be
retrieving over its own source).

| mode | n | score /12 | tokens in | tokens out | sec | $/question | verified cites | bogus cites |
|---|---|---|---|---|---|---|---|---|
| baseline | 5 | **6.60** | 225,986 | 7,090 | 67.4 | 0.5229 | 42 | 3 |
| llm-tldr | 5 | **2.40** | 5,179 | 1,325 | 13.6 | 0.0236 | 2 | 29 |
| voitta-rag | 5 | **3.00** | 4,255 | 1,384 | 14.7 | 0.0223 | 22 | 3 |

Per question class (mean /12):

| class | baseline | llm-tldr | voitta-rag |
|---|---|---|---|
| implementation-lookup | 9 | 5 | 3 |
| path-tracing | 5 | 1 | 1 |
| change-planning | 4 | 3 | 1 |
| architecture | 4 | 2 | **6** |
| edge-case-dependency | 11 | 1 | 4 |

**voitta-rag scores low for a different reason than llm-tldr, and the citation column shows
it.** llm-tldr fabricated locations (29 bogus / 2 verified). voitta-rag's citations are
almost all real (22 verified / 3 bogus) — it scores low because it **retrieved the wrong
documents and then correctly refused**. Four of five answers are refusals whose stated
reason is that the source files were not in the retrieved context. The judge notes confirm
the premise is false: the files are in the repo.

Mechanism: the folder was indexed whole, and BM25+semantic retrieval on questions phrased
in changelog vocabulary ("malformed start tags", "charset conflict") ranks `CHANGES.md` and
`change-archive.txt` above the `.java` files, because jsoup's changelog literally describes
these behaviours in prose. The Java source was indexed and reachable; it just lost the
ranking. A refusal is the *safe* failure mode and is worth distinguishing from a confident
wrong answer — but it is still a retrieval failure, and it is the mode's headline result.

The one class where voitta-rag wins outright is **architecture** (6 vs 4 baseline, 2 tldr),
which is the class where prose-level chunks are the right context.

### Corpus asymmetry — read the comparison with this caveat

The three arms do not see the same corpus. `baseline` and `llm-tldr` are scoped to
`**/*.java` minus tests (97 files); voitta-rag indexed the whole checkout (233 files,
7,819 chunks) including Markdown and changelogs. That asymmetry is *load-bearing* for the
result above, so this is not yet a clean head-to-head. Two follow-ups, in order of value:

1. Re-run voitta-rag against a `.java`-only index and see how much of the gap is corpus
   rather than retrieval.
2. voitta-rag chunk records carry `chunk_index` but no line numbers, so the answering model
   cannot cite `file:line` from a chunk even when the chunk is correct. Its 22 verified
   citations came from reasoning about file paths, not from the retrieval payload. Adding
   line spans to the chunk record is the highest-value change to this arm.

### Second harness bug found by this run

`baseline` is **not** a full repository dump, and the earlier "full-dump ceiling" reading of
its 7.25 was wrong. `build_baseline` packs files alphabetically and *skips* any file that
would exceed the remaining character budget while continuing down the list — so the packing
is biased against large files. At `baseline_char_budget: 600000` it includes **69 of 97
files**, and the ones it drops are the biggest: `Parser.java`, `Tokeniser.java`,
`TokeniserState.java`, `TreeBuilder.java`, `HtmlTreeBuilder.java`, `HtmlTreeBuilderState.java`
are all absent, while seven small `parser/` files are present.

That is why the re-run of `jsoup-q4/baseline` (the architecture question — "describe the
tokeniser, the tree builder, and the parser state machine") scored 4/12: the answer states
that those exact classes are "absent from the provided files", which is true of the dump it
was given and false of the repository. The judge scored it against the repository and marked
3 bogus citations.

So the baseline arm currently measures "as much of the repo as fits, largest files dropped
first", not "the whole repo". Raise the budget above the ~1.1 MB the 97 files need, or change
the packer to stop at the budget rather than skip-and-continue, before treating baseline as
the quality ceiling.

## First run — 2026-07-27 (baseline + llm-tldr only)

`results/runs-20260727T224039Z-scored.jsonl`. jsoup @ `d24b16d9`, answers on
Claude Sonnet 5 (effort high), judge Claude Opus 5 (effort high) with repo tools.

| mode | n | score /12 | tokens in | tokens out | sec | $/question |
|---|---|---|---|---|---|---|
| baseline | 4 | **7.25** | 225,986 | 7,128 | 67.0 | 0.5233 |
| llm-tldr | 5 | **2.40** | 5,179 | 1,325 | 13.6 | 0.0236 |

Per question class (mean /12):

| class | baseline | llm-tldr |
|---|---|---|
| implementation-lookup | 9 | 5 |
| path-tracing | 5 | 1 |
| change-planning | 4 | 3 |
| architecture | (lost) | 2 |
| edge-case-dependency | 11 | 1 |

**The headline is the citation column, not the token column.** llm-tldr cut input
tokens 44x and cost 22x, and produced **29 bogus citations against 2 verified**
across five questions -- zero verified citations in three of them. Baseline
produced **34 verified and 0 bogus**. A context strategy that saves 97% of the
tokens and cites locations that do not exist has not saved anything; it has moved
the cost from tokens to review. This is exactly what the benchmark existed to
catch, and a token-savings-only comparison would have reported the opposite
conclusion.

Mechanism: `tldr semantic search` returns `"line": 1` for every code unit, so the
answering model had no real line numbers and invented them. See the fairness
caveat below before generalising this to llm-tldr as a whole.

Absolute scores are low on both arms. Baseline at 7.25/12 is not a good result
either -- the full-dump ceiling on this question set is unimpressive, which is
itself worth knowing before treating baseline as the quality bar.

### Harness bug found by this run (fixed; see also the second bug above)

`jsoup-q4 / baseline` returned **no text at all** and could not be scored. With
adaptive thinking on, `max_tokens` caps thinking **plus** response text; at
`max_tokens: 16000` the model spent the entire budget inside thinking on a
226K-token context and emitted zero text blocks -- billed in full, `stop_reason:
max_tokens`, empty answer. `answer_max_tokens` is now **32000**. That cell was re-run
on 2026-07-31 and the baseline column is complete in the second-run table above; the
table immediately below still reports baseline at n=4 rather than silently averaging
four cells as if they were five.

Raising `answer_max_tokens` to 32000 then broke the runner a second way: the SDK refuses
a non-streaming request it estimates may run longer than ten minutes (`ValueError:
Streaming is required for operations that may take longer than 10 minutes`), so every
cell failed identically on the next run. `answer_one` now uses
`client.messages.stream(...)` + `stream.get_final_message()`, which reports the same
`usage` fields.

## Known limits of v0

- **The llm-tldr arm measures one way of using the tool, not its ceiling.** The
  adapter calls `tldr semantic search --expand` only. `tldr context`, `structure`,
  `calls`, and `slice` may return real line numbers and would likely score very
  differently on citation accuracy. Do not read the result above as "llm-tldr is
  bad" -- read it as "this adapter, on this question set, produced uncitable
  context." Trying a second adapter is a one-function change in `modes.py` and is
  the highest-value next experiment.
- **The voitta-rag arm likewise measures one way of using the tool.** Whole-folder index,
  default `sparse_weight`, top-20 chunks, no line numbers in the chunk record. Each of
  those is a knob, and the corpus asymmetry against the two `.java`-scoped arms is not yet
  controlled for. See "Corpus asymmetry" above.
- **`baseline` is a size-biased subset, not a full dump** -- 69 of 97 files at the current
  budget, largest files dropped. It is not currently a quality ceiling. See "Second harness
  bug" above.
- One repo, and an unfamiliar one. It does not measure the "we already know this
  codebase" case that the familiar-repo arm in the plan covers.
- Five questions is enough to catch a large effect and not enough to rank close
  results. Treat a small gap between modes as unresolved, not as a tie.
- Single judge, single pass, no inter-rater check.
- CCE and the two chain modes from hq#75 are not in v0, so v0 does **not** by
  itself close hq#75.
