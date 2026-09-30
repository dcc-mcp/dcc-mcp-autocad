# dcc-mcp-autocad

AutoCAD adapter for the [DCC Model Context Protocol](https://github.com/dcc-mcp/dcc-mcp-core)
ecosystem — typed DWG automation for agents, portable-first.

## Why this adapter is different

`dcc-mcp-autocad` is the ecosystem's first **portable-first** adapter. Locating
the host never depends on a registered COM ProgID, so green/portable installs
work, and the headless `accoreconsole.exe` path keeps batch automation usable
even where COM is unavailable.

Two interchangeable transports implement one contract:

| Transport | Host | Capabilities |
|---|---|---|
| `com` | interactive AutoCAD | live document, window handle, interactive selection, batch edit, plot |
| `accoreconsole` | `accoreconsole.exe` (headless) | batch edit, plot |

The bridge negotiates the richest available transport at runtime and reports the
resulting capability set.

## Capability degradation

When only the headless transport is usable, these capabilities are unavailable
and are **declared** by `get_status` rather than failing silently:

- **no live document session** — drawings open and save per operation
- **no window handle** — UI targeting is unavailable
- **no interactive selection** — pass typed entity arguments instead

## Install

```bash
pip install dcc-mcp-autocad
# on Windows, for the interactive COM transport:
pip install "dcc-mcp-autocad[windows]"
```

Verify the host:

```bash
dcc-mcp-autocad-doctor doctor --json
dcc-mcp-autocad-doctor verify --json --transport accoreconsole
```

## Host discovery

Resolution order — no step requires COM registration:

1. `AUTOCAD_EXE` environment variable
2. Registry install location
3. Known installation directories (`C:\Program Files\Autodesk\AutoCAD <year>`)

`accoreconsole.exe` is resolved as a sibling of `acad.exe`. COM ProgIDs are an
optional accelerator with a version-tolerant fallback chain
(`AutoCAD.Application.25.1` → `25` → `24.3` → unversioned), never a prerequisite.

## Tools

The bundled `autocad-drawings` skill exposes:

- `get_status` — discovery, transport, and active capabilities
- `inspect_drawing` — bounded entity, layer, and layout metadata
- `create_drawing` — new DWG from a template
- `manage_layers` — ensure layers exist
- `add_entities` — append typed lines, circles, points, or text

Entity arguments are JSON data only; callers cannot supply source code.

## Runtime shape

An **external bridge host**: the adapter's Python runs *outside* AutoCAD and
drives it over the negotiated transport. AutoCAD ships no embedded Python at
all, so the AutoCAD process imposes no constraint on the interpreter.

That is a statement about the **host**, not about this package. This package's
own floor is `requires-python = ">=3.9"`, and it is set under the ecosystem's
standing host-side exception — the same one behind `openusd >=3.9`,
`photoshop >=3.8`, and `zbrush >=3.10`. It is **not** a signal that the Python
3.7 floor held by `dcc-mcp-core` and the shared libraries has moved; it has
not.

## Compatibility

- AutoCAD 2021+ (ObjectARX 24.x / 25.x)
- Verified against AutoCAD 2026 (`25.1s`)
- Vanilla AutoCAD COM surface only — no vertical (Architecture/Mechanical/
  Electrical) or OEM-specific APIs are used

## License

MIT
