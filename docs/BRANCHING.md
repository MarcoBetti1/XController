# Branching Workflow

This repository currently uses two long-lived branches with different responsibilities.

## Branch Roles

- `main`
  Clean integration branch. Keep only the reusable controller package, packaging files, and docs here.
- `labui-testing`
  Development branch for `lab_ui`, `lab_api`, manual walkthrough tooling, and automated tests.

Downstream consumers should update from `main`, not from `labui-testing`.

## Daily Workflow

Sync `main` first:

```bash
git switch main
git pull origin main
```

Refresh the lab branch from the clean core branch:

```bash
git switch labui-testing
git rebase main
```

Do lab UI and test work on `labui-testing`.

When a controller fix from `labui-testing` should ship to consumers, move only the relevant commit(s) back to `main`:

```bash
git switch main
git cherry-pick <commit-sha>
```

Avoid merging `labui-testing` into `main` as a full branch merge. A full merge would reintroduce `lab_ui`, `lab_api`, and test-only files into the clean branch.

## Suggested Rules

- Keep `main` fast-forwardable and releasable at all times.
- Treat `labui-testing` as disposable development support for the core package.
- If a change belongs only to manual validation or local experimentation, keep it on `labui-testing`.
- Tag releases from `main` after CI passes.
