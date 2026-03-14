# x_controller

`x_controller` is a click-first Playwright library for automating common X.com workflows with a persistent browser profile.

It is designed for reuse from scripts, services, dashboards, and test harnesses instead of a single hard-coded workflow.

## What It Supports

- Persistent Chromium profile sessions
- Login checks and login-page handoff
- Home/timeline recovery
- Timeline, search, mentions, and visible-post reads
- Post, view, like, reply, follow, and unfollow actions
- Verified delete flows for own posts, replies, and reposts
- Bulk cleanup helpers for posts, replies, reposts, or all content
- Post metric scraping
- A lab API/UI for manual method validation

## Installation

Core library only, from inside this directory:

```bash
python -m pip install -e .
playwright install chromium
```

Core library only, from the parent directory:

```bash
python -m pip install -e ./x_controller
playwright install chromium
```

Lab API/UI dependencies are optional. Install them with the `lab` extra:

```bash
python -m pip install -e ".[lab]"
```

If you also want the lab stack from the parent directory:

```bash
python -m pip install -e "./x_controller[lab]"
```

The packaging metadata is configured so this flat package layout installs as `x_controller`, with the lab stack behind an optional extra.

## Quick Start

```python
import asyncio

from x_controller import XController


async def main() -> None:
    controller = XController(profile_path="data/profiles/default_profile")
    await controller.start()
    try:
        if not await controller.is_logged_in():
            await controller.open_login_page()
            return

        posts = await controller.search_posts("browser automation", limit=5)
        if posts:
            await controller.view_post(posts[0].platform_post_id)
    finally:
        await controller.close()


asyncio.run(main())
```

## Public API

Primary exports:

- `x_controller.XController`
- `x_controller.XTextAdapter`
- `x_controller.ControllerSettings`
- `x_controller.ObservedPostData`
- `x_controller.ControllerLabManager`

Compatibility aliases:

- `XController` and `XTextAdapter` are the same class.
- `comment_post()` and `reply_to_post()` are both supported. `reply_to_post()` is the clearer canonical name for X.

## Project Layout

- `adapter.py`: main X controller implementation
- `base.py`: shared adapter contract and post model
- `settings.py`: runtime settings and configuration normalization
- `human.py`: human-like timing and mouse/typing helpers
- `lab.py`: manual lab orchestration
- `lab_api.py`: FastAPI wrapper around the lab manager
- `lab_ui.py`: Streamlit UI for lab actions
- `docs/API.md`: method-level API notes and compatibility guidance
- `docs/ARCHITECTURE.md`: internal structure and extension points
- `CHANGELOG.md`: tracked cleanup and migration notes

## Lab Tools

Install the lab extra before starting these commands:

```bash
python -m pip install -e ".[lab]"
```

Start the lab API:

```bash
python -m x_controller.lab_api --host 127.0.0.1 --port 8010
```

Start the lab UI:

```bash
streamlit run x_controller/lab_ui.py
```

If you are already inside the package directory, `streamlit run lab_ui.py` works as well.

## Notes For Reuse

- The controller is stateful and intended to be reused across multiple actions inside one started session.
- Most flows prefer UI clicks and only fall back to direct URL navigation when recovery is needed.
- Sync Playwright fallback is preferred on Windows to avoid event-loop/subprocess issues.
- Methods that mutate state should be treated as best-effort browser automation, not transactional API calls.

## Additional Docs

- [API reference](docs/API.md)
- [Architecture notes](docs/ARCHITECTURE.md)
- [Change log](CHANGELOG.md)
