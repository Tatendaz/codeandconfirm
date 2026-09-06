# Real backend (`kind = "firebase-real"`)

The local emulator cannot show you production security rules, real Auth behaviour, network latency,
App Check, or Cloud Functions as deployed. `firebase-real` runs the same review against a **real
Firebase project** — and only ever against the *development* project.

## The three-environment rule

| Environment | Who may create or delete data | CodeAndConfirm |
|---|---|---|
| **dev** (e.g. `my-app-dev`) | anyone, including automation; nothing here is precious | may create and delete anything under its own account prefix `cac-<run-id>-` |
| **test** (e.g. `my-app-test`) | humans only: release rehearsals, manual QA, the nightly CI smoke | **refused** — never listed in `allowed_project_ids` |
| **prod** | end users | **never** |

The rule is enforced, not assumed:

1. The run swaps the checkout's client configs with the repo's own script (`scripts/use-firebase-env.sh dev`).
2. The active project id is read back **from the swapped files** (`PROJECT_ID` in the iOS plist,
   `project_info.project_id` in the Android JSON). Both must agree.
3. That id must appear in `[backend].allowed_project_ids`. Anything else — including an empty allow-list —
   restores the placeholder configs and ends the run **BLOCKED** (`real backend refused: …`). No build, no
   device, no worker runs against the wrong project.
4. The cleanup credential must be a **service-account key of that same project**
   (`…@<project-id>.iam.gserviceaccount.com`). A key for another project, or a personal user credential,
   is refused, so the sweep physically cannot reach test or prod.

## Configuration

Committed (`codeandconfirm.toml`, placeholders only):

```toml
[backend]
kind = "firebase-real"
env_script = "scripts/use-firebase-env.sh"    # run as `<script> dev`; `<script> emulator` restores
env_name = "dev"
restore_arg = "emulator"
allowed_project_ids = ["my-app-dev"]          # the development project ONLY
config_files = ["ios/App/GoogleService-Info.plist", "android/app/google-services.json"]
prepare = ["cd firebase/functions && npm install --no-audit --no-fund"]
# health defaults to the Firestore + Identity Toolkit endpoints

[backend.cleanup]
command = "cd firebase/functions && APP_PROJECT_ID={project_id} APP_SWEEP_PREFIX={account_prefix} APP_STORAGE_BUCKET={storage_bucket} APP_SWEEP_STRICT=1 npm run --silent cleanup:test-accounts"
verify  = "cd firebase/functions && APP_PROJECT_ID={project_id} APP_SWEEP_PREFIX={account_prefix} APP_SWEEP_DRY_RUN=1 npm run --silent cleanup:test-accounts"
```

Machine-local (`.codeandconfirm.local.toml`, gitignored — these paths are personal and hold credentials):

```toml
[backend]
config_source = "~/Projects/my-app/firebase/config/dev"     # the real client configs; the repo gitignores them
[backend.cleanup]
credentials = "~/.codeandconfirm/secrets/my-app-dev-service-account.json"   # dev-only key, outside every repo
```

`codeandconfirm doctor` checks all of it: the allow-list (and warns when a name looks like test or prod),
the swap script, the config source, the credential's project, the cleanup command, Node, and any run still
flagged for cleanup.

### Placeholders added for this kind

`{project_id}`, `{account_prefix}` (`cac-<run-id>-`), `{storage_bucket}`, `{credentials}` — usable in the
cleanup commands and their `env`.

## What a run does

- **backend phase** — stages the real configs from `config_source` into `config_dest`
  (default `firebase/config/<env_name>`), runs the swap script, verifies the project id, checks the cloud
  endpoints answer. The two swapped tracked files are whitelisted in the pristine check (the swap is the
  coordinator's change, not the worker's). The base build gets the same swap so base comparisons talk to
  the same project.
- **workers** are told, in the task, that this is a shared real development project, that every account
  must start with `cac-<run-id>-`, and that they must not touch anything else. Several runs can share the
  project because their prefixes differ.
- **before the gate** — the cleanup command deletes every account whose email starts with the prefix and
  all of its data (trees with their people, events, memories and photos; profile document; email index;
  pending invites). The `verify` command then re-lists the prefix; it must find **0**.
- **report** — the header says `backend: real project <id>`, the gate has an advisory `backend.cleanup`
  check, and the run's section "Real backend cleanup" lists what was removed.
- **after the run** — the placeholder configs are restored in the run's worktrees and the staged real
  configs are deleted.

## When cleanup fails

Cleanup is housekeeping: it never changes the product verdict. It is, however, deliberately loud.
If the sweep cannot run, exits non-zero, prints no `SWEEP_RESULT` line, or the verification still finds
accounts:

- the report starts with `⚠️ NEEDS CLEANUP …` (before the gate table);
- the coordinator logs `NEEDS CLEANUP: real project … may still hold accounts …`;
- a durable marker is written to `$CODEANDCONFIRM_HOME/needs-cleanup/<run-id>.json`, which
  `codeandconfirm status` and `codeandconfirm doctor` show until it is cleared;
- `codeandconfirm cleanup <run-id>` retries the sweep with the run's configuration snapshot and clears the
  marker on success. `codeandconfirm cancel` also runs the sweep for a real-backend run.

## Sweep contract

The repo owns the deletion logic (it knows its own data model); CodeAndConfirm only requires:

- the `command` deletes exactly the accounts whose email starts with `{account_prefix}` and everything
  they own, and prints as its last line `SWEEP_RESULT {"users": N, "trees": M, "left": K}`;
- the `verify` command deletes nothing and prints `SWEEP_RESULT {"matched": N}`;
- both read `GOOGLE_APPLICATION_CREDENTIALS` (exported by CodeAndConfirm) and exit non-zero on error.

A reference shape: a Node script on the Firebase Admin SDK that pages through Auth users, selects those whose
email starts with the prefix, deletes each user's owned documents (and their Storage prefix), their profile and index
documents, and finally the Auth user; `APP_SWEEP_PREFIX`, `APP_SWEEP_STRICT=1` and `APP_SWEEP_DRY_RUN=1` switch its behaviour.

## Costs and etiquette

Real projects meter reads, writes, Auth operations and Storage. A run creates a handful of accounts and a
few dozen documents — negligible on the free tier — but the sweep exists so they never accumulate. Do not
point `allowed_project_ids` at a project that other people rely on for demos or release sign-off.
