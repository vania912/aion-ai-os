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
