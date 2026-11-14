# Connect a project to Research Workspace

Research Workspace is installed separately from your project. You need Python
3.11 or newer and Git. The examples use `python`; use your configured Python
interpreter if that command is unavailable. Replace `<app-install>` with the app's
installation directory. No Python dependencies need to be downloaded for the
local launcher.

## Start the app

From any directory, run:

```text
python <app-install>/workspace.py serve
```

The default address is `http://127.0.0.1:8771/`. Local app data is stored under
`%LOCALAPPDATA%/ResearchWorkspace` on Windows, or
`~/.local/share/research-workspace` elsewhere. Use `--home <directory>` for an
explicit data location and `--port <port>` for another loopback port. An existing
project database is not automatically copied or replaced by this command.

## Preview and apply project setup

Use the repository root, not a nested source directory:

```text
python <app-install>/workspace.py setup --repo <project-root>
python <app-install>/workspace.py setup --repo <project-root> --apply
python <app-install>/workspace.py doctor --repo <project-root>
```

Use `--url http://127.0.0.1:<port>` on setup and doctor if the app uses another port.
The first command shows the files that setup proposes without contacting the
service or writing project files. The second registers a project and its local
repository connection through the service, then writes:

- `.research-workspace/project.json`: portable project identity, name and project-specific remote binding;
- `.research-workspace/schema.json`: versioned storage/portable-record contract;
- `.research-workspace/AGENTS.md`: the project-agent integration contract.

Repeated setup reuses the project identity. A clone must retain its existing
manifest to join the same research project. Local repository paths remain in the
app's database and are not written into the portable manifest. Existing integration
instructions are not overwritten if they differ from the generated guide.

Add a reference to `.research-workspace/AGENTS.md` in your project's existing
agent instructions, preserving unrelated content. Review and commit those three
integration files using your normal Git workflow. The app does not commit or push
them automatically.

## Use the workspace safely

Open the app to select your project. Use scoped API/CLI transactions for tasks,
claims, evidence and journals. The compatibility HTTP client is available as
`python <app-install>/tools/roadmap_cli.py --url <service-url> --help`; its `v1-read`
and `v1-transaction` commands support project-scoped work. Actor labels on the
local service are trusted labels, not authentication.

Never edit SQLite directly, copy live WAL files as a backup, merge database files,
or infer verified software behavior from workflow completion or hints. Preserve
source hashes, methods and limitations when reporting evidence.

## Synchronization

Project setup and automatic Git source refresh are implemented. Enable portable
[Git sharing](git-sharing.md) explicitly for each connected project. Optional
[live hosting](self-hosting.md) supports authenticated reconciliation and Git
fallback; its configuration/review APIs are available before the complete browser
controls. Check connection status and resolve conflicts through reviewed previews.
A successful repository connection alone does not establish synchronized research.

Snapshot synchronization does not transfer active claims as shared leases. Local
clients report local-only claims; use the host directly for shared coordination.
Full journal text sharing remains under development, even when portable event
metadata and record synchronization report current.
