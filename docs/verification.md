# Verify Collaboration Workbench

Use Python 3.11+, Git and Node. Test data is synthetic and disposable; tests do
not require an existing SSG2 service, production database or private evidence.
Run commands from the application checkout. The app itself has no third-party
Python dependencies.

## Regression and scale

```text
python -B -X utf8 -m unittest discover -s tests
```

This includes the default 20,000 committed-token Git/two-live-client roundtrip.
The extended scale gate is explicit. In PowerShell:

```powershell
$env:WORKBENCH_SCALE_GATE = '1'
python -B -X utf8 -m unittest tests.test_workspace_large_sync.SourceRoundtripTests -v
```

For the Node controller, renderer and joined authenticated HTTP suites:

```powershell
$env:WORKBENCH_TEST_PYTHON = (Get-Command python).Source
Get-ChildItem tests/workspace_*.cjs, tests/task_graph_view.cjs, tests/repository_panel.cjs |
  ForEach-Object { node $_.FullName; if ($LASTEXITCODE -ne 0) { throw "Failed: $_" } }
```

Use the actual Python executable if `python` is a Windows Store alias. These
checks need permission to launch disposable loopback servers and local Git
subprocesses. A denied socket is an environment failure, not a passing check.

## Authenticated browser acceptance

Install Playwright as an optional development dependency (it is not needed to run
the app): `npm install --no-save --package-lock=false playwright`. Either install
its Chromium with `npx playwright install chromium`, or select an installed
browser with `WORKBENCH_BROWSER_CHANNEL=msedge` or `chrome`. If Playwright is
already installed elsewhere, set `NODE_PATH` to that installation's node_modules.

```powershell
$env:WORKBENCH_TEST_PYTHON = (Get-Command python).Source
node tests/source_criticism_browser.cjs
```

The runner starts and shuts down a disposable authenticated production HTTP
handler. Fixture credentials travel only over the runner's private pipe and
remain outside Git. A real browser exercises conflicting builds and both exact
hashes/locators, source entry links, explicit parent/run authoring and wrong-input
rejection, shared-origin grouping, historical/archive/withdrawal states, literal
text, visible limitations, partial-coverage searches, a 390px layout, draft guards,
lost-response recovery with byte-identical retry, explicit stale reconciliation,
and owner-only two-repository setup with project-isolated evidence.

The single source-criticism fixture and two-repository browser workflow complement
the broader Python Git/live, project-contract, authorization and recovery suites.
They establish application behavior with synthetic inputs; they cannot establish
native provenance or switch a real project's production authority. An SSG2
cutover still requires its own owner-coordinated backup and acceptance.
