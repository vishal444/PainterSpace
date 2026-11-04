# PainterSpace queue requests

When the user asks to process a pending PainterSpace queue request, act only as the LLM tool-call endpoint:

1. Read `task_queue/pending_request.json`. Use only the user prompt and the function catalog, scene data, and asset data supplied with that request to choose calls. Do not inspect, search, or analyze the codebase or consult any separate catalog, scene, or asset files.
2. Write only a `{"calls": [{"function": "...", "args": {...}}]}` response to `task_queue/llm_response.json` and return the same JSON in chat.
3. Stop. Do not perform any separate analysis, planning, compilation, validation, simulation, rendering, animation bake, codebase check, or other computation for that pending request. Do not run the queue processor or any scripts. PainterSpace executes the response.

An empty action timeline does not cancel the prompt. Choose tool calls from the prompt, treating unassigned drawn paths as optional guides. Only explicit timeline segments constrain the shot.

# Python environment for engineering work

The OpenUSD Python package `pxr` is installed in this project's virtual environment at `.venv/lib/python3.12/site-packages/pxr`. Use `.venv/bin/python` for Python commands and `.venv/bin/python -m pytest` for tests; the system `python3` and `pytest` may not find `pxr`.

# General engineering rule: no hardcoded solutions

Across the entire codebase, do not solve tasks with special cases or fixed values chosen for one example. This applies to algorithms, control flow, identifiers, paths, data formats, UI behavior, assets, scenes, prompts, and tests. Derive behavior from the relevant inputs and explicit contracts, and make variable behavior configurable or data-driven. Use named constants only for genuine domain rules, with their purpose documented.

Design logic for reuse and scale across different inputs and workflows. Do not add branches that recognize a particular example merely to make it pass. When the available information is insufficient for a general solution, report the limitation clearly instead of silently substituting a case-specific workaround. Verify changes against multiple representative cases, including cases beyond the one that exposed the issue.

# Documentation for significant changes

When making a significant codebase change, update `README.md` in the same task. This includes new or substantially changed features, user workflows, public APIs, and architecture. Explain what changed and how to use it where relevant. Small fixes and internal refactors that do not affect documented behavior do not require a README update.

The queue-request instructions above still take precedence when processing a pending request: write only the tool-call JSON and stop.
