---
name: autocad-drawings
description: >-
  Create, inspect, and edit AutoCAD DWG drawings through a negotiated host
  transport (interactive COM or headless accoreconsole.exe). Use for bounded
  layer management, entity authoring, and drawing inspection workflows. Do not
  use for driving the interactive AutoCAD GUI.
license: MIT
compatibility: "Python 3.9+; AutoCAD 2021+ (ObjectARX 24.x/25.x); dcc-mcp-core >=0.20.36"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: autocad
    layer: domain
    version: "0.1.2"  # x-release-please-version
    search-hint: "AutoCAD DWG drawing layer entity line circle batch portable headless accoreconsole"
    tags: [autocad, dwg, cad, drafting, aec, batch, portable]
    tools: tools.yaml
---

# AutoCAD Drawings

Drive DWG files through typed tools. The adapter runs outside the AutoCAD
process and selects a transport at runtime:

- **COM** (interactive) — full capability: live document sessions, window
  handle, interactive selection, batch edits.
- **accoreconsole.exe** (headless) — batch edits only. Used automatically when
  COM is unavailable, including on portable/green installs that register no
  COM ProgID.

## Capability degradation

When the headless transport is active these capabilities are **unavailable**,
and `get_status` reports them explicitly instead of failing silently:

- no live document session — drawings open and save per operation
- no window handle — UI targeting is unavailable
- no interactive selection — pass typed entity arguments instead

Call `get_status` before a workflow that depends on an interactive capability.

## Usage

1. `get_status` — confirm the transport and capability set.
2. `create_drawing` or `inspect_drawing` — establish or read a target DWG.
3. `manage_layers` — create layers before placing entities on them.
4. `add_entities` — append typed geometry and save.

Entity arguments are JSON data only; callers cannot supply source code.

## Write verification

`create_drawing`, `add_entities`, and `manage_layers` re-open the DWG and read
the change back from disk before returning. A result carrying `verified: true`
was read back, not assumed: the in-memory document looks identical whether or
not the save reached the file. When the read-back disagrees the call raises an
error naming the check, the expected value, the value actually read, and the
host `ACADVER` — treat that as a real failure, not a retry candidate.
