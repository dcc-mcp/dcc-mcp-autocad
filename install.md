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
| `40` | runtime verification failed |

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

## Notes

- `accoreconsole.exe` is driven over **stdin** with CRLF line endings. The
  `/s <script>` flag is unreliable on AutoCAD 2026 and is not used.
- COM is STA: the adapter initialises COM per thread and serialises host calls.
- Late binding (`win32com.client.dynamic.Dispatch`) is used throughout; the
  makepy `gen_py` cache has been observed to be incomplete.
