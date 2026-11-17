# Registering a local original source

Workers can authenticate a local source without uploading its bytes or editing
the database. From the connected project checkout, run the installed app tool:

```powershell
python <app-install>/tools/workbench_source.py TASK relative/path/to/source --root-id EXISTING_ROOT --artifact-id PROPOSED_ID --kind encrypted-plist --build ORIGINAL_BUILD
```

Use the root already bound to this checkout. The path must stay inside the
project repository. The command hashes the file locally, then the service checks
that hash against its bound root before registering an immutable version. A
different bound file rejects the entire transaction, including its task journal.
The existing artifact identity is reused when its root and path match. Conflicting
identity, kind or immutable version metadata require investigation, not overwrite.
An expired pagination cursor or revision conflict requires rereading current state.

The result supplies `artifact_version_id` for `workbench_worker.py discover
TASK --version ID ...`. Registration records source identity; it establishes no
behavior or parity. Findings still need a locator, original execution or inspection
method, input scope and limitations. No source bytes are sent by this client.

Worker briefs expose `claim`, `claim_state` and `reserved_paths`. A successful
claim can leave workflow `status` as `ready`; ownership and workflow status are
separate. Verify `claim.by` before concluding that a claim failed.
