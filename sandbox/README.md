# Coder sandbox image

The image scripts from the **Coder agent** (Stage 13D, [HLD §18.6](../HLD.md))
run in. It is a *sibling* of the workload under investigation:

- **Podman** — `podman run --rm --network container:<target>` joins the
  target's network namespace (its ports, its DNS view) but shares **no
  filesystem and no process table**. If the target isn't running, the
  sandbox runs on the default network.
- **Kubernetes** — `kubectl debug <pod> --image=… --attach` attaches an
  ephemeral debug container to the pod.

## Build

```bash
podman build -t poddebugger-coder-sandbox sandbox/
```

The engine defaults to `localhost/poddebugger-coder-sandbox:latest`
(what the build above produces). Point `PODDEBUGGER_CODER_IMAGE` at a
registry copy once you push one — for Kubernetes the cluster must be able
to pull it.

## What's inside

bash, python3 (+requests), curl, wget, jq, dig/nslookup (bind-tools),
nc, and the postgres / mariadb / redis CLI clients. Runs as a non-root
user (`coder`, uid 10001).

## Safety model

The image is deliberately *not* the safety boundary — the approval gate
is: every script is shown in full (`kind="code"`, risk `high`) before it
runs, non-TTY runs deny by default, and a persistent rule can only
pre-approve one exact `(language, script-hash)` pair — never "all code".
The sandbox adds containment (no target filesystem, non-root), not
permission.
