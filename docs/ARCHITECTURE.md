# Architecture Notes

## Design Intent

The package is structured around one stateful adapter that owns:

- Playwright startup and shutdown
- browser/profile recovery
- shared DOM interaction helpers
- X-specific navigation and action flows

The library favors click-driven navigation first and uses direct URL navigation as a recovery path.

## Current Modules

- `adapter.py`
  Main controller implementation and X-specific flow logic.
- `base.py`
  Shared adapter contract and the `ObservedPostData` model.
- `settings.py`
  Runtime knobs for browser size, typing cadence, pauses, and user-agent defaults.
- `human.py`
  Helper methods for jitter, typing cadence, mouse movement, and network-idle waits.
- `lab.py`
  Session orchestration and action runner for manual verification.
- `lab_api.py`
  FastAPI transport over the lab manager.
- `lab_ui.py`
  Streamlit frontend for exercising controller actions manually.

## Adapter Organization

Inside `XController`, methods now fall into clearer buckets:

- startup/shutdown
- sync fallback helpers
- low-level DOM helpers
- navigation helpers
- post collection/parsing helpers
- public read/write actions

The main cleanup rule applied here was: keep the public behavior stable, but consolidate duplicate internal logic where possible.

## Compatibility Choices

- `XController` remains an alias of `XTextAdapter`.
- `comment_post()` is still supported, but both public reply methods share one internal implementation now.
- Existing lab endpoints were kept so manual validation workflows do not break.

## Packaging

This repository uses a flat package layout where the package root is the project root itself.

`pyproject.toml` now explicitly maps the package name `x_controller` to `.` so editable installs and wheels do not depend on implicit setuptools discovery.

## Recommended Next Steps

- Split non-X-specific browser helpers into a reusable internal module if another platform adapter is added.
- Add integration tests around mocked DOM snapshots or controlled test pages before broadening the action surface further.
- Introduce semantic version tags once the API is stable enough for third-party consumers.
