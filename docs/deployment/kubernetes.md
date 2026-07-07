# Kubernetes

The [Helm chart](helm.md) is the supported way to run Memcove on Kubernetes — server +
Flight + reconciler, probes, ServiceAccount/IRSA, ConfigMap, and optional Ingress and
NetworkPolicy. This page covers the **network isolation** that the chart can enable but
that you must get right regardless of how you deploy. Everything environment-specific is a
`MEMCOVE_*` setting (see [Settings reference](../configuration/settings.md)).

## The trust-boundary NetworkPolicy

Because Memcove trusts a header set by the proxy, the **only** thing that may reach its
ports is that proxy. This policy denies all ingress to the Memcove pods except from the
proxy, for the MCP port (8090) and the Flight port (8815). Adapt the labels, namespace,
and ports to your cluster. Restrict Trino similarly (a separate policy) so impersonation
can't be sidestepped.

```yaml title="deploy/networkpolicy.example.yaml"
--8<-- "deploy/networkpolicy.example.yaml"
```

!!! warning "Restrict Trino too"
    Locking down the Memcove pods is not enough. If tenants (or anything else) can reach
    Trino directly, per-tenant impersonation can be sidestepped. Apply an equivalent policy
    in your Trino namespace so only Memcove can reach it.

## Values surface

When you install with the [Helm chart](helm.md), configuration lives in its
`values.yaml` (and the NetworkPolicy above is a chart toggle — `networkPolicy.enabled`).
If you'd rather build your own manifests, this flat reference maps every knob to its
`MEMCOVE_*` env var; secrets should come from your secret manager, not committed config.

```yaml title="deploy/values.example.yaml"
--8<-- "deploy/values.example.yaml"
```

## Deploying

The [Helm chart](helm.md) wires up the three entry points for you. If you deploy by hand,
the container commands are:

- `memcove-server` — the MCP control plane (Streamable HTTP, port 8090).
- `memcove-flight` — the Arrow Flight data plane (gRPC, port 8815), only needed if you use
  the [streaming tools](../tools/streaming.md).
- `memcove-reconcile` — registry/catalog drift repair (run as a CronJob).

See the [Production checklist](checklist.md) before exposing anything.
