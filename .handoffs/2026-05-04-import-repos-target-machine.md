---
created: 2026-05-04
audience: AI agent
status: Ready to execute
slug: import-repos-target-machine
---

# Handoff: Run import_repos.py on the target machine

**Created:** 2026-05-04
**Author:** Claude Code session (source machine)
**For:** AI Agent (running on the target machine that will receive the imported repos)
**Status:** Ready to execute

---

## Summary

The voitta-rag bulk-import round-trip has just been validated end-to-end on the source machine. A clean export JSON exists at `/tmp/repos.json` on the source host (29 github repos: 23 ssh on github.com, 6 token on git.zoominfo.com, branches preserved). Your job: receive that file on the target machine, fill in the secrets that were intentionally not exported, and run `scripts/import_repos.py` against the target's voitta-rag instance.

## Project Context

- **Repo:** `voitta-ai/voitta-rag` (private)
- **Branch:** `import-docs` — PR #16 (https://github.com/voitta-ai/voitta-rag/pull/16)
- **Origin tip:** `b9bdab6` (just pushed in this session) on top of `069e259`
- **Stack:** FastAPI + SQLAlchemy (aiosqlite) + Qdrant; deployed via Docker Compose; default port mapping 58000:8000
- **Scripts under test:** `scripts/export_repos.py` (source machine) and `scripts/import_repos.py` (target machine — your job)
- **JSON schema:** `{ hosts: { <hostname>: { auth_method, [username, token] } }, folders: { <parent>: [{ repo, branch }, ...] } }`

## The Plan

Run the import on the target machine. Concretely:

1. Make sure the target machine is on `origin/import-docs` at `b9bdab6` or later. Old tips of this branch were force-pushed away, so a `git pull` on a stale local clone may need `git fetch && git reset --hard origin/import-docs`.
2. Make sure voitta-rag is running on the target. Docker Compose: `make docker-build && make docker-up`. Default reachable at `http://localhost:58000`.
3. Receive `/tmp/repos.json` from the source machine (e.g., `scp source:/tmp/repos.json /tmp/repos.json`).
4. Edit `/tmp/repos.json` to add credentials per the "Secrets" section below. **Do not commit this edited file.**
5. Run the importer with the right user selected. (See "Multi-user gotcha" below — the importer has the same single-user limitation the exporter had until today, see "Risks" below.)
6. Verify repos appear in the UI and sync to `synced` status.

## Key Files

| File | Why It Matters |
|------|---------------|
| `scripts/export_repos.py` | Just patched (commit b9bdab6) — source side, you don't run it but read it to understand the JSON schema |
| `scripts/import_repos.py` | What you're running on the target |
| `scripts/import_repos.example.json` | Reference for the JSON shape |
| `/tmp/repos.json` | Will be transferred to target. Contains 29 repos across 3 folders (`clickagy`, `marketing-cloud`, `pex`); two host entries (`github.com` ssh, `git.zoominfo.com` token); both lack credentials |
| `.env` (target) | Importer reads `VOITTA_HOST`, `DOCKER_PORT` from here. If absent, defaults to `localhost:58000` |
| `.claude/skills/voitta-rag-export-import-gotchas/SKILL.md` | Project-scoped skill with the gotchas you need (also referenced below). Currently UNTRACKED on the source — you may want to read the contents below since it may not be in the target's worktree |

## Current State

**Done (on the source machine, just now):**
- Restarted the source's voitta-rag container to clear an aiosqlite "disk I/O error" on `/api/sync/{path}` (classic macOS Docker bind-mount + SQLite WAL hazard). Other endpoints continued working — only this table was affected. Restart was the fix.
- Patched `scripts/export_repos.py` and committed as `b9bdab6` on `import-docs`. Pushed to origin. Three fixes:
  - Multi-user picker support via `VOITTA_USER=<id-or-name>` env var.
  - Distinguish "no sync source" (HTTP 200 + `null` body) from real errors (non-2xx, non-JSON); count and surface errors; exit non-zero on any error.
  - Always preserve the source branch in the export (was previously dropping `main`/`master`/`develop` and relying on importer auto-detect, which could flip `develop` → `main`).
- Ran `VOITTA_HOST=localhost DOCKER_PORT=58000 VOITTA_USER=2 python3 scripts/export_repos.py /tmp/repos.json` — clean output, 29 github repos / 3 folders / 2 hosts. Verified `clickagy/portal` carries `branch: develop` (the case the old auto-detect would have broken).

**In progress:**
- Transferring `/tmp/repos.json` to the target machine (your machine).

**Not started (your work):**
- Add credentials to the JSON.
- Run `scripts/import_repos.py` on the target.
- Verify imports + sync.
- Merge PR #16.

## Decisions Made

- **Always export branch name** — previously dropped `main`/`master`/`develop` because the importer would auto-detect. But importer auto-detect prefers `main > master > develop > first`, which doesn't match what the source actually had. A folder explicitly on `develop` could end up on `main` if the remote has both. The source is authoritative; preserve it. (commit b9bdab6)
- **Don't export secrets, ever** — `username`/`token`/`ssh_key` are deliberately stripped. Caller fills them in on the target. This is load-bearing, not just a "nice to have." Don't extend the exporter to write tokens into the JSON.
- **Restart container instead of recreating SQLite DB** — the aiosqlite "disk I/O error" symptom on the source was a stale-`-shm`/WAL view inside the container, not file corruption. Host-side `sqlite3 voitta.db "PRAGMA integrity_check"` returned `ok` and the host could read every row. `docker restart` cleared it. Don't reach for `chmod 777` or DB rebuilds.
- **Project skill not committed** — `.claude/skills/voitta-rag-export-import-gotchas/SKILL.md` was written on the source but left untracked, since `.claude/` in this worktree currently holds local-only files (`settings.local.json`, prior handoff doc). If you want it in the branch, that's a small follow-up commit.

## Important Context

### Risk: importer has the same single-user bug the exporter had

The exporter's `ensure_user()` was patched in this session to handle multi-user instances. **The importer's `ensure_user()` was NOT patched** — it still has the original behavior: silently warns and proceeds when `GET /` returns 200 (multi-user picker). If the target machine has more than one user in voitta-rag, the importer will fail to establish a session and either error out or apply changes as the wrong user. Before running the import:

```bash
# Check user count on target
curl -sS http://localhost:58000/ | grep -c 'user-form'
```

If that prints > 1, you need to either:
- (a) Apply the same patch to `scripts/import_repos.py` that was applied to `export_repos.py` in commit b9bdab6 (parse picker, honor `VOITTA_USER`), OR
- (b) Pre-establish a cookie before running the script. Ugly but workable: pre-POST `/select-user/<id>` with a `requests.Session()` and pass that cookie via env. Simpler: ensure the target has exactly one user.

This is the most likely thing to bite you. Plan for it.

### Risk: `/api/sync/{path}` may 500 on the target too

If the target is also macOS host + Docker bind-mount + SQLite (very likely, since this is the supported deployment), it can hit the same intermittent "disk I/O error". Symptoms:
- API endpoints that read `folder_sync_sources` table return HTTP 500
- `docker logs <voitta-rag-container>` shows `sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) disk I/O error`
- Host-side `sqlite3` on the same DB file works fine

Fix: `docker restart <voitta-rag-container>` (or `docker compose restart`). If recurring, see `~/.claude/skills/sqlite-disk-io-error-docker-bind-mount/SKILL.md` (user-wide skill, written in this session).

### Security: API leaks PAT in plaintext

`GET /api/sync/{folder}` returns the GitHub PAT in cleartext in the JSON response body. Be careful with logs, transcripts, screenshots. Don't paste raw API responses anywhere.

### Branch behavior

Every entry in `/tmp/repos.json` has an explicit `branch`. The importer's `query_default_branch` is now only invoked when an entry is missing `branch`, which shouldn't happen with the patched exporter's output. If you see auto-detection happen on a non-trivial number of entries, the JSON came from an older exporter — re-export.

## Next Steps

1. **Sync the target's checkout to `b9bdab6` or later.**
   ```
   cd <voitta-rag-on-target> && git fetch origin && git reset --hard origin/import-docs
   git --no-pager log --oneline -3   # b9bdab6 should be HEAD
   ```
   Acceptance: `git log` shows `b9bdab6 Make export_repos.py multi-user aware...`.

2. **Bring up voitta-rag on the target.**
   ```
   make docker-build && make docker-up
   curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:58000/
   ```
   Acceptance: HTTP 200 from `/`.

3. **Probe `/api/sync/...` to confirm DB is healthy.**
   ```
   curl -sS -o /dev/null -w "%{http_code}\n" http://localhost:58000/api/folders
   ```
   Acceptance: 200. If 500: `docker restart` per the SQLite skill, then retry.

4. **Check user count, decide whether to patch the importer.**
   ```
   curl -sS http://localhost:58000/ | grep -c 'user-form'
   ```
   - 0 → not on the picker page (probably already redirected to /browse — fine).
   - 1 → single user — `import_repos.py` will work as-is.
   - 2+ → patch `import_repos.py` to honor `VOITTA_USER`, mirroring the exporter changes in commit b9bdab6 (specifically: `parse_users`, `pick_user`, and the picker branch in `ensure_user`). Or pre-create the session cookie.

5. **Receive and edit the JSON.**
   ```
   scp source:/tmp/repos.json /tmp/repos.json
   ```
   Edit `/tmp/repos.json`. The `hosts` section will look like:
   ```json
   "hosts": {
     "github.com": {"auth_method": "ssh"},
     "git.zoominfo.com": {"auth_method": "token"}
   }
   ```
   For `github.com` (ssh): nothing to add — the container reads `~/.ssh` from the host bind-mount. Make sure that mount exists in `docker-compose.yml` and the key is loaded.
   For `git.zoominfo.com` (token): add username and a fresh PAT:
   ```json
   "git.zoominfo.com": {
     "auth_method": "token",
     "username": "<gregory's git.zoominfo.com username>",
     "token": "<fresh PAT>"
   }
   ```
   **The previous PAT was accidentally exposed in cleartext to a Claude Code session and must be rotated before being reused.** The user has been told to rotate it. Use the rotated token here.

6. **Run the importer.**
   ```
   VOITTA_HOST=localhost DOCKER_PORT=58000 \
     python3 scripts/import_repos.py /tmp/repos.json
   ```
   Watch for: `[N/29] <folder>/<repo>` per line; expect `Synced OK` for each.

7. **Verify.**
   - Check the UI at `http://localhost:58000/browse` — should see all 29 repos under `clickagy`, `marketing-cloud`, `pex`.
   - Spot-check `clickagy/portal` is on `develop` (not `main`).
   - Spot-check a `git.zoominfo.com` repo synced (verifies token auth path).
   - Final summary line should be `Done. 29/29 repos processed: 29 synced, 0 skipped, 0 failed.`

8. **Merge PR #16** once verification passes.

## Constraints

- **Don't write secrets to the repo.** `/tmp/repos.json` is for local use only. Don't `git add` it. The `.gitignore` already excludes `scripts/import_repos_personal.json` but `/tmp/repos.json` is outside the repo so the safety is your discipline, not a gitignore rule.
- **No `--no-verify` on git commits.** Repo-wide rule from CLAUDE.md.
- **No emojis in code.** OK in markdown (this handoff is fine).
- **`return retval` style.** If you patch `import_repos.py`, follow the existing exporter pattern: assign to `retval`, return `retval`.
- **Specific imports only**, no wildcards.
- **Don't add tests** unless the user explicitly asks.
- **Don't add features beyond what step 4 in Next Steps requires.** YAGNI.
- **Use git worktrees** if you're going to do parallel work.
- **`--no-pager` on git commands.** Repo-wide rule.

## Reference: the patch shape that fixed `export_repos.py` multi-user

If step 4 in Next Steps requires patching `import_repos.py`, mirror this. The exporter's `ensure_user` now does:

```python
USER_FORM_RE = re.compile(
    r'action="/select-user/(?P<id>\d+)".*?<span class="user-name">(?P<name>[^<]+)</span>',
    re.DOTALL,
)

def parse_users(html):
    return [(int(m["id"]), m["name"].strip()) for m in USER_FORM_RE.finditer(html)]

def pick_user(users):
    if not users: return None
    selector = (os.environ.get("VOITTA_USER") or "").strip()
    if selector:
        if selector.isdigit():
            for uid, name in users:
                if uid == int(selector): return uid, name
        for uid, name in users:
            if name == selector: return uid, name
        return None
    if len(users) == 1: return users[0]
    return None  # ambiguous — error out

def ensure_user(session, base_url):
    resp = session.get(f"{base_url}/", allow_redirects=False)
    if resp.status_code == 302:
        session.get(f"{base_url}{resp.headers['Location']}")
        return True
    if resp.status_code != 200:
        return False
    pick = pick_user(parse_users(resp.text))
    if pick is None: return False
    uid, _ = pick
    sel = session.post(f"{base_url}/select-user/{uid}", allow_redirects=False)
    if sel.status_code != 302: return False
    session.get(f"{base_url}{sel.headers['Location']}")
    return True
```

See commit `b9bdab6` for the full shape including error printing.
