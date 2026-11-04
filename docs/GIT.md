# Git service

Code: `src/aios/services/git.py`, `src/aios/adapters/git_cli.py`, models in `src/aios/domain/git.py`.
Read-only. Built on the [policy engine](POLICY.md).

## Operations

| Operation | Behaviour |
|---|---|
| `get_recent_changes(since, limit, path)` | Newest-first commits (max 100) with the policy-visible files each changed |
| `get_commit(ref)` | Message, visible files and patch for one commit (patch capped at 40,000 characters and 50 files) |
| `get_diff(base, head, path)` | Diff between two commits, visible files only |
| `search_history(query, mode, since, limit)` | `message` (fixed-string grep, case-insensitive), `content` (pickaxe `-S`), or `path` |
| `get_branch_status()` | Branch or detached HEAD, upstream, ahead/behind, staged/unstaged/untracked counts, visible entries |
| `list_branches(remotes, limit)` | Local (or remote) branch names with hashes |

CLI: `aios git recent|show|diff|search|status|branches <project> ...`.

## The central rule: git must not become a side door around the file policy

A repository's history can contain files the policy would never let you read (a committed
`.env`, a key, a binary). So:

- **Changed-file lists and patches are built only from allowed paths.** Withheld files are counted
  (`hidden_files`) but never named. Patches are requested with an explicit list of allowed paths.
- **`rev:path` is impossible.** Refs containing `:` are rejected, so `HEAD:.env` cannot read a blob.
  Refs must also not start with `-`, contain `..`, whitespace or control characters. Every ref is
  resolved with `<ref>^{commit}`.
- **Pickaxe search is not an oracle.** Unrestricted `git log -S<guess>` would reveal whether a
  guessed secret exists in a denied file, by whether any commit comes back. Content search is
  limited by pathspec to allowed file types and excludes denied names and directories. A test
  first proves that unrestricted pickaxe *does* find the planted secret, then that the service
  returns nothing.
- **Path arguments** (`path`, `search_history(mode="path")`) go through `vet_repo_path`: same
  deny-list, extension and syntax rules as file reads, checked lexically (deleted files work).
- Pathspecs built from user paths use `:(literal)` magic so globs cannot expand.

## Hardened execution

| Measure | Why | Evidence |
|---|---|---|
| Subcommand allowlist (`log show diff diff-tree status rev-parse for-each-ref`) | nothing that writes or networks can run | 28 refused subcommands tested |
| Options such as `--output`, `-O`, `--no-index`, `-c`, `-C`, `--git-dir`, `--ext-diff`, `--textconv` refused before `--` | file writes, reads outside the repo, program execution | tested per option |
| `GIT_*` variables from the parent process removed | a poisoned `GIT_DIR`, `GIT_EXTERNAL_DIFF`, ... cannot redirect git | tested with a decoy repository |
| `-c core.fsmonitor=false` | a repo config can make plain `git status` run a program (verified with git 2.43.0: the program ran) | tested: marker file not created |
| `--no-ext-diff --no-textconv` | `diff.external` / `diff.<driver>.textconv` run programs | tested (premise checked: unhardened git runs them) |
| `--no-optional-locks` + `GIT_OPTIONAL_LOCKS=0` | `git status` otherwise rewrites the index | tested: index bytes unchanged (premise checked) |
| `GIT_CEILING_DIRECTORIES` = parent of the project root | an enclosing repository is never picked up | tested; the service also refuses a root with no repository |
| Timeout (30 s) and stdout cap, stdin closed | hangs, huge output | tested with fake git scripts (POSIX only) |
| Error messages replace the project path with `<root>` | no path leakage | tested |

## Limitations

- **Project root must be the repository root** (a `.git` entry directly inside it). Projects that
  are a subdirectory of a repository are refused, not supported.
- **Renames are reported as delete + add** (`--no-renames`), for deterministic parsing.
- **Merge commits** are not specifically tested; their diff output may be empty or combined.
- **Commit messages, author names and branch names are returned unfiltered.** They are untrusted
  text (they can contain pasted secrets or prompt-injection attempts). The MCP layer should
  label them as data.
- **Allowed files can still contain secrets in their content**; diffs show them, as `read_file`
  would. There is no content scanning or redaction.
- **Content-search exclusions use git glob matching**, which differs from the policy's `fnmatch`
  in places (for example `*` does not cross `/`). Tests cover `.env`, `.env.local`, `*.pem` and a
  binary type; the other patterns are not individually tested.
- **Over-blocking:** paths the policy refuses lexically (a `:` in a name, names shaped like 8.3
  aliases, reserved Windows device names) are hidden from history views even if they are harmless.
- **Platform coverage:** developed and tested on Linux with git 2.43.0. The Windows CI jobs run
  the same tests (except POSIX-only fake-git script tests); see the CI results for the commit that
  added this service. Whether `GIT_CEILING_DIRECTORIES` accepts the Windows path form was not
  checked independently of those tests.

## Test evidence

- 360 tests pass locally (Linux, Python 3.13, git 2.43.0), using real repositories whose history
  includes a committed-then-deleted `.env`, a commit touching only a hidden file, a `.pem`, a
  binary, an empty repository, a detached HEAD, and an upstream with ahead/behind counts.
- Manual mutation checks: each of the following makes the suite fail: removing the fsmonitor
  override, `--no-textconv`, `--no-ext-diff`, `--no-optional-locks`, the visibility filter on
  changed files, the pathspec restriction on pickaxe or on patches, the `:` ban in refs, the
  repository-discovery ceiling, the `GIT_*` scrub, the `--output` block, and the deny/extension
  checks in `vet_repo_path`. Two of these (`:` in refs, `GIT_*` scrub) initially **survived**
  because my first tests were too weak (the `:` case was also stopped by a second layer; the
  `GIT_*` test used a decoy repository that failed either way). They were rewritten and are caught.

## The write runner (Phase 4a)

`aios.adapters.git_exec.GitWriteRunner` is the only code that lets aios change a project's `.git`
(worktrees and branches for plan execution, see [PHASE4-PLAN.md](PHASE4-PLAN.md)). It is not a
pass-through like the read-only runner above: callers use typed methods (`resolve_commit`,
`worktree_add`, `worktree_remove`, `branch_delete`, `branch_exists`, `worktree_list`, `changes`) and
the argument lists are built from validated values. It never pushes, fetches, commits, merges or
checks out in the main working tree. **Nothing calls it yet**: `aios plan start` arrives in step 4b.

Why it needs its own hardening (observed 2026-11-04, git 2.43): a plain `git worktree add` ran the
repository's `post-checkout` hook and its smudge filter, and a repository's long-running `process`
filter also ran unless it was overridden too. Creating a worktree would execute project-controlled
code. Measures, each with a test against a repository that tries to run its own script:

- hooks are redirected to an empty directory; every `filter.<name>.{smudge,clean,process,required}`
  found in the repository's configuration, including files pulled in by `include.path`, is overridden
  (pass-through `cat`, empty `process`, `required=false`). Consequence: LFS or other filters do not
  run, so such files are checked out as stored, which is fine for scope checking and wrong for
  building the project in that worktree;
- system and user git configuration are not read; every `GIT_*` variable from the parent is dropped;
- `core.fsmonitor` is off; diffs never use external diff programs or textconv; submodule recursion,
  auto-gc and network protocols are disabled; stdin is closed; a timeout kills the whole process
  group (otherwise a hook or filter holding the output pipe delayed the error); output is capped;
- only branches named `aios/PLAN-nnnn` are created or deleted; only worktrees registered with such a
  branch are removed (never the main working tree, never an arbitrary directory); the worktree path
  must be absolute, outside the project, and not exist;
- `changes()` snapshots a worktree against a base commit without touching the worktree: it copies the
  index, adds untracked files *intent-to-add* to the copy, and reports changed paths (added,
  modified, deleted, type-changed, with modes and a binary flag), git-ignored files (a fresh worktree
  has none, so any present are new), and a SHA-256 of the full binary diff. Awkward names (spaces,
  newlines, non-UTF-8) are handled with NUL-separated output.

Not covered / not verified: on Windows the hostile-repository tests pass in CI, but the `taskkill`
tree kill and symlink/mode handling are not exercised there; git features the tests do not exercise;
empty directories (git does not track them).
