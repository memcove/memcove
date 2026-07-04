# Kubernetes

Memcove ships **example** manifests to adapt, not a baked deployment. Wire them into your
own Helm chart, ArgoCD Application, or Kustomize base, pointing at your object store,
Trino, Postgres, and identity proxy. Everything environment-specific is a `MEMCOVE_*`
setting (see [Settings reference](../configuration/settings.md)).

## The trust-boundary NetworkPolicy

Because Memcove trusts a header set by the proxy, the **only** thing that may reach its
ports is that proxy. This policy denies all ingress to the Memcove pods except from the
proxy, for the MCP port (8090) and the Flight port (8815). Adapt the labels, namespace,
and ports to your cluster. Restrict Trino similarly (a separate policy) so impersonation
can't be sidestepped.

```yaml title="deploy/networkpolicy.example.yaml"
--8<-- "deploy/networkpolicy.example.yaml"
```

## Values surface

The full config surface as a values file — every key maps to a `MEMCOVE_*` env var.
Secrets (S3 keys, PG password, ticket secret) should come from your secret manager, not
this file. Only override what you need; defaults live in the app.

```yaml title="deploy/values.example.yaml"
--8<-- "deploy/values.example.yaml"
```

## Deploying

Point these at your infrastructure and deploy with whatever your platform uses. The two
container entry points are:

- `memcove-server` — the MCP control plane (Streamable HTTP, port 8090).
- `memcove-flight` — the Arrow Flight data plane (gRPC, port 8815), only needed if you use
  the [streaming tools](../tools/streaming.md).

See the [Production checklist](checklist.md) before exposing anything.
