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

**Working with drawings you already have:** the adapter is deny-by-default — a
path is accepted only when it resolves inside the configured workspace (by
default the system temp directory) **or** inside one of the roots listed in
`DCC_MCP_AUTOCAD_ALLOWED_ROOTS`, which may lie outside the workspace. Point it
at your DWG archive before editing existing drawings — see
[Drawing workspace](install.md#drawing-workspace-deny-by-default):

```cmd
set DCC_MCP_AUTOCAD_WORKSPACE=C:\drawing-work
set DCC_MCP_AUTOCAD_ALLOWED_ROOTS=C:\Users\you\Documents
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

## Host contract: the two sides

The adapter installs nothing into AutoCAD — no plug-in, no Autoloader bundle,
no `NETLOAD`. Everything below describes the two sides of one boundary.

**Host side (inside AutoCAD).** AutoCAD ships no embedded Python and none is
added. Nothing in this package executes inside the AutoCAD process; the adapter
only drives it through AutoCAD's own automation surfaces (vanilla COM and
AutoLISP over the core console). There is consequently no host-side Python to
version-manage.

**Bridge side (outside AutoCAD).** The adapter's Python runs in its own
interpreter and talks to AutoCAD over one of two transports:

| | COM | `accoreconsole.exe` |
|---|---|---|
| Package | `pip install "dcc-mcp-autocad[windows]"` (pywin32) | nothing extra |
| Session | attaches to a live, interactive AutoCAD | launches headless per operation |
| Script delivery | method calls | CRLF script piped through **stdin** |
| Result | return values | JSON file written by AutoLISP, read back by the transport |
| Needs a visible seat | yes | no |

Constraints that are real and load-bearing:

- **`accoreconsole.exe /s <script>` does not work** on AutoCAD 2026 — it hangs
  without running the script. The transport always pipes through stdin with CRLF
  line endings instead.
- **COM is STA.** The adapter initialises COM per thread and serialises host
  calls, which is why throughput is ~500 ops/s rather than per-call latency.
- **Late binding only** (`win32com.client.dynamic.Dispatch`). The makepy
  `gen_py` cache has been observed to be incomplete — `IAcadLine` was missing
  `Color`.
- **Busy hosts are retried, bounded.** Only `RPC_E_CALL_REJECTED` and
  `RPC_E_SERVERCALL_RETRYLATER` are retried, at most 30 times × 0.5 s. Any other
  HRESULT propagates on the first call.
- **Vanilla COM surface only.** No vertical (Architecture/Mechanical/Electrical)
  or OEM-specific API is referenced; a test fails the build if one appears.

## Compatibility

Support is decided by a machine-readable matrix (`compat_matrix.json`) that
ships inside the wheel, not by a flat "2021+" claim. The doctor reads the host's
real `ACADVER` and reports the verdict for it.

| AutoCAD | ACADVER | Status |
|---|---|---|
| 2021 | 24.0 | declared, unverified |
| 2022 | 24.1 | declared, unverified |
| 2023 | 24.2 | declared, unverified |
| 2024 | 24.3 | declared, unverified |
| 2025 | 25.0 | declared, unverified |
| **2026** | **25.1** | **verified** |
| 2027 | 25.2 | projected, unverified |

Only AutoCAD 2026 has machine evidence behind it: out-of-process COM round-trips,
LISP dispatch read back through `USERR1`, a 200-entity batch, a `SaveAs` → reopen
cycle, and a headless `accoreconsole.exe` DWG cross-checked through COM. Every
other row is declared so the doctor can name it, not because it passed anything.

**An unverified build is refused by default.** That is the point: a seat
registering `AutoCAD.Application.25` proves the ProgID binds, not that the
contract passes. The doctor exits `11` with an `error_code` naming the verdict.
To accept the risk on a specific machine:

```bash
set DCC_MCP_AUTOCAD_ALLOW_UNVERIFIED_HOST=1
```

The report then carries `host.override_active: true`, so the override is visible
in the payload rather than hidden in the behaviour. An `ACADVER` that cannot be
parsed at all is never overridable — there is no range to opt into.

## Write verification

A mutating tool returns only after it has re-opened the DWG and read the change
back from disk. `create_drawing`, `add_entities`, and `manage_layers` all do
this on both transports; the in-memory document is not evidence, because it
looks identical whether or not the save reached the file.

When a read-back disagrees the call raises an error that names the tool, the
check, the expected value, the value actually read, and the host `ACADVER` —
never a bare failure. Success carries `verified: true`.

## Verification scope — read before quoting CI

**No GitHub-hosted runner has AutoCAD installed or licensed, so CI never
launches the host.** A green CI run is contract-level evidence: import safety,
transport contracts, capability logic, and the write-back contract above. It is
not host-level evidence, and this project does not present it as such.

Host-level evidence comes from `dcc-mcp-autocad-doctor verify --json` on a
machine with a licensed AutoCAD, or from the `autocad-live` CI job, which is
written for a self-hosted runner and disabled by default. The doctor report
states this bound itself, under `verification`.

## License

MIT
