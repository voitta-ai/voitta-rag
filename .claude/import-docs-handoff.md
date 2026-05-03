# import-docs PR handoff

Last updated: 2026-04-10 (session 2)

## Current state

- Branch: `import-docs`
- PR: https://github.com/voitta-ai/voitta-rag/pull/16
- Status: History rewritten + force-pushed; export functionality added;
  pending real end-to-end test across two machines

## Commits on branch (4, on top of master 2dd74a8)

1. `9e6ed96` Add README TOC and expand Bulk Repository Import docs
2. `8e244d8` Fix dotenv loader to strip inline comments
3. `c2378f0` Add make docker-build to Quick Start
4. `7aa20cc` Add export_repos.py and document round-trip workflow

(Previous tips `8b652b9` / `78ee084` / `1a32c14` are orphaned on origin
after force-push-with-lease.)

## Completed this session (session 2)

- Identified that `scripts/import_repos_personal.example.json` was
  byte-identical to the real personal config (73 private-org repos) and
  should never have been committed
- Dropped the file entirely from the branch history via
  `git reset --hard 9e6ed96^` + `git rm` + `git commit --amend` +
  `git cherry-pick` (avoided `-i` per repo rules)
- Updated `README.md` in the amended commit to stop referencing the
  personal example (single example file now: `import_repos.example.json`)
- Added `scripts/export_repos.py` -- inverse of `import_repos.py`.
  Walks `/api/folders` + `/api/sync/{path}`, collects github sources
  into the import schema. Never writes secrets.
- Added new README subsections: "Exporting from a running instance"
  and "Round-trip between machines"
- Force-pushed `import-docs` (force-with-lease)

## Remaining work

- Real end-to-end test across machines:
  1. On a different machine with an existing voitta-rag instance:
     `python3 scripts/export_repos.py /tmp/repos.json`
  2. `scp /tmp/repos.json` to this machine
  3. Here: `make docker-build && make docker-up`
  4. `python3 scripts/import_repos.py /tmp/repos.json`
  5. Verify repos appear and sync
- Merge PR #16 once verified

## Decisions made (session 2)

- Git history: chose "amend + force-push" (over "new commit only" or
  "leave history") so the 73 private-org repo list is no longer
  reachable from `origin/import-docs`. Note: GitHub may still serve
  old SHAs until gc; full scrubbing would need filter-repo + GH
  support contact.
- Sanitized example: decided to simply *delete* the personal example
  rather than replace with placeholder content, since
  `scripts/import_repos.example.json` already exists as the minimal
  template and YAGNI says we don't need two examples.
- Export secrets: never write username/token. User re-enters on
  target machine. Matches the pattern of `auth_method` being the only
  host-level field in the committed example.
- Export scope: only `github` source type. Walks 2 levels deep
  (`parent/repo`), which matches what `import_repos.py` creates.
  Non-github sources counted and reported, not exported.

## Files changed vs origin/master (4 files, +283/-8)

- `.gitignore` (+1) -- `scripts/import_repos_personal.json`
- `README.md` (+89/-7) -- TOC (with export subsections), expanded
  Bulk Import section, gdrive mapping example, `make docker-build`
  in Quick Start, export + round-trip subsections
- `scripts/import_repos.py` (+3) -- strip inline comments in
  `load_dotenv()`
- `scripts/export_repos.py` (new, 198 lines) -- the inverse script

## Resume action

1. `cd /Users/gregory/g/git.voitta/voitta-rag && git checkout import-docs`
2. Produce `/tmp/repos.json` from another voitta-rag instance via
   `python3 scripts/export_repos.py /tmp/repos.json`
3. `scp` it here, then:
   `make docker-build && make docker-up`
4. `python3 scripts/import_repos.py /tmp/repos.json`
5. If clean, merge PR #16
