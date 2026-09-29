# Installing dcc-mcp-autocad

The adapter installs no host plug-in. Verification is read-only.

```bash
python -m pip install "dcc-mcp-autocad[windows]"
dcc-mcp-autocad-doctor doctor --json
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | host verified and directly usable |
| `10` | not ready — no usable transport found |
| `11` | host answered, but its version is outside the compatibility matrix |
| `40` | runtime verification failed |

`11` is separate from `10` on purpose: "I could not reach AutoCAD" and "AutoCAD
answered and I am refusing it" need different responses. The report also carries
an `error_code` (`autocad_host_version_unverified`, `..._newer_than_matrix`,
`..._unlisted`, `..._unparsable`, `..._unavailable`) so callers can branch
without parsing prose.

## Locating AutoCAD

Discovery never requires a COM ProgID. Order:

1. `AUTOCAD_EXE` — explicit path to `acad.exe`
2. Registry install location
3. `C:\Program Files\Autodesk\AutoCAD <year>`

`accoreconsole.exe` is taken as a sibling of `acad.exe`. Override it with
`AUTOCAD_ACCORECONSOLE_EXE`.

On portable/green installs, point `AUTOCAD_EXE` at the unpacked `acad.exe`
(or `AUTOCAD_ACCORECONSOLE_EXE` at `accoreconsole.exe` for headless-only use).

## Transports

```bash
# force the interactive COM transport
dcc-mcp-autocad-doctor verify --json --transport com

# force the headless core-console transport
dcc-mcp-autocad-doctor verify --json --transport accoreconsole
```

Without `--transport` the richest available transport is negotiated
automatically, preferring COM.

## Headless mode and degraded capabilities

With `--transport accoreconsole` (or when COM is unavailable) the report sets
`degraded_mode: true` and lists `degraded`. Absent capabilities:

- no live document session
- no window handle
- no interactive selection

Batch operations — open, edit layers and entities, save, plot — remain fully
available. This is the intended mode for DWG batch processing and for
portable installs.

## Host compatibility

Support is decided by `compat_matrix.json`, which ships inside the wheel. The
doctor reports the host's real `ACADVER` under `host`, together with the
verdict:

```bash
dcc-mcp-autocad-doctor doctor --json
```

Only AutoCAD 2026 (ACADVER `25.1`) is verified. Every other declared year is
refused by default. To run on one of them anyway:

```bash
set DCC_MCP_AUTOCAD_ALLOW_UNVERIFIED_HOST=1   # Windows cmd
$env:DCC_MCP_AUTOCAD_ALLOW_UNVERIFIED_HOST=1  # PowerShell
export DCC_MCP_AUTOCAD_ALLOW_UNVERIFIED_HOST=1
```

The override never applies to an `ACADVER` that could not be parsed, and it is
always reported back as `host.override_active: true` — so a supervising agent
can see the run was accepted rather than verified.

## Drawing workspace (deny-by-default)

`create_drawing`, `add_entities`, and `manage_layers` only accept paths inside
the configured workspace. The default workspace is the system temp directory,
so **reading or editing drawings you already have — for example
`%USERPROFILE%\Documents\plan.dwg` — is rejected until you extend the roots**:

```bash
set DCC_MCP_AUTOCAD_WORKSPACE=C:\drawing-work
set DCC_MCP_AUTOCAD_ALLOWED_ROOTS=C:\Users\you\Documents
```

`DCC_MCP_AUTOCAD_ALLOWED_ROOTS` accepts an `os.pathsep`-separated list (`;` on
Windows, `:` elsewhere). The deny-by-default rule is deliberate, but it is also
the first obstacle that appears when the adapter is pointed at an existing DWG
archive, so it is documented here rather than left to be discovered.

## Verification scope

CI never launches AutoCAD: no GitHub-hosted runner has it installed or licensed.
A green CI run is contract-level evidence only — import safety, transport
contracts, capability logic, and the post-write read-back. Host-level evidence
requires running `verify --json` on a machine with a licensed AutoCAD, or
enabling the self-hosted `autocad-live` job. The doctor restates this bound in
every report under `verification`.

## Notes

- `accoreconsole.exe` is driven over **stdin** with CRLF line endings. The
  `/s <script>` flag is unreliable on AutoCAD 2026 and is not used.
- COM is STA: the adapter initialises COM per thread and serialises host calls.
- Late binding (`win32com.client.dynamic.Dispatch`) is used throughout; the
  makepy `gen_py` cache has been observed to be incomplete.
