# Work directly against a shared host

Workers can use the same hosted authority without configuring a second local
Workbench or synchronizing its database. Keep the project's existing
`.research-workspace/project.json` in the local checkout. Save the host's
project-scoped credential in a private file outside repositories, then run:

```text
python tools/workbench_worker.py --repo <project-checkout> --url https://workbench.example --token-file <private-token-file> brief <task-uuid>
```

The same options apply to `claim`, `discover`, `watch`, `finish` and `resume`.
`RESEARCH_WORKSPACE_TOKEN_FILE` is also supported. HTTPS requests use the existing
certificate-verifying, redirect-refusing client. Tokens are not written into
project configuration, briefs or recovery journals. Local loopback HTTP remains
available. Remote authenticated HTTP is refused.

Browser sign-in stores a tab session; it does not create a credential file for
another agent process. The private file must exist on the worker's computer.
Hosted task access does not grant filesystem access, remote command execution,
GitHub permissions, or automatically connect that worker's checkout to the host.
Code changes still use its own clone and Git credentials. Local-only source-root
registration may require a coordinated host-side step; do not invent host paths
or create another project identity to work around it.
