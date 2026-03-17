# CI/CD Instructions

This repository should validate the clean package branch before any downstream project pulls from it.

## CI

Use the GitHub Actions workflow at `.github/workflows/ci.yml`.

It is intended to run on pushes and pull requests for:

- `main`
- `labui-testing`

The workflow does three things:

- installs the project with whatever extras are defined on that branch
- runs an import smoke test for the public package
- builds the wheel and sdist

If the branch contains a `tests/` directory, it also runs `pytest`.

That means:

- `main` gets package-level verification without carrying lab or test files
- `labui-testing` can run the broader validation stack when those files exist there

## CD

There is no automatic deployment step configured here. The recommended release flow is manual and branch-driven:

1. Land or cherry-pick releasable controller changes onto `main`.
2. Wait for CI on `main` to pass.
3. Build release artifacts locally if needed:

```bash
python -m pip install --upgrade pip build
python -m build
```

4. Tag the release from `main`:

```bash
git tag v0.1.0
git push origin main --tags
```

5. Update the downstream project from `main` or from the new tag, depending on how tightly you want to pin updates.

## Practical Policy

- Do not deploy from `labui-testing`.
- Do not merge `labui-testing` into `main` wholesale.
- Use `main` as the source of truth for anything another repository imports.
