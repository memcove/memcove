# Install with Helm

Memcove ships a Helm chart that deploys the whole service — the MCP control plane, the
Arrow Flight data plane, and the reconciler CronJob — wired to the built-in health
probes. It's **bring-your-own-infra**: you point it at your own object store, Iceberg
REST catalog, Trino (≥ 431), and registry DB. See [BYO Trino & catalog](byo-trino.md) for
what Memcove assumes of those.

## Get the chart

The chart is published as an OCI artifact and is also in the repo. Either works:

=== "From the registry"

    ```bash
    helm install memcove oci://ghcr.io/memcove/charts/memcove \
      --version 0.3.1 -f my-values.yaml
    ```

=== "From a clone"

    ```bash
    git clone https://github.com/memcove/memcove
    helm install memcove ./memcove/deploy/charts/memcove -f my-values.yaml
    ```

Render locally first to review what you'll apply:

```bash
helm template memcove ./deploy/charts/memcove -f my-values.yaml | less
```

## Minimal values

Override only what your environment needs; the rest have defaults. A realistic
`my-values.yaml` pointing at AWS:

```yaml
image:
  tag: ""   # defaults to the chart's appVersion (the matching release)

serviceAccount:
  # AWS IRSA: the pod assumes this role for S3, so no static keys are needed.
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/memcove

config:
  s3:
    region: us-east-1
    pathStyle: false
    warehouseBucket: my-memcove-warehouse
    stagingBucket: my-memcove-staging
    artifactsBucket: my-memcove-artifacts
  iceberg:
    restUri: http://iceberg-rest.data.svc:8181
    warehouse: s3://my-memcove-warehouse/
  trino:
    host: trino.data.svc
    port: 443
    httpScheme: https
    impersonation: true

secrets:
  # Pre-created Secret holding MEMCOVE_PG_DSN + MEMCOVE_FLIGHT_TICKET_SECRET (and S3 keys
  # if you're not using IRSA). Strongly preferred over chart-managed secret values.
  existingSecret: memcove-secrets
```

### Secrets

Sensitive settings never belong in values. Create a Secret and reference it with
`secrets.existingSecret`. It must hold the `MEMCOVE_*` keys the app reads:

```bash
kubectl create secret generic memcove-secrets \
  --from-literal=MEMCOVE_PG_DSN='postgresql://memcove:***@pg.data.svc:5432/memcove' \
  --from-literal=MEMCOVE_FLIGHT_TICKET_SECRET="$(openssl rand -hex 32)"
  # add MEMCOVE_S3_ACCESS_KEY / MEMCOVE_S3_SECRET_KEY only if NOT using IRSA
```

`MEMCOVE_REGISTRY_DSN` also works here if you run the registry on SQLite/MySQL rather
than Postgres — it takes precedence over `MEMCOVE_PG_DSN`. See
[registry backends](../configuration/settings.md).

### AWS credentials (IRSA)

Set the role ARN on `serviceAccount.annotations` and leave the S3 keys **empty** —
Memcove then uses the pod's IAM role via the AWS default credential chain. No static keys
in the cluster. (The keyless fallback also covers instance profiles and STS off-cluster.)

## Enable native OAuth

To let a client like Claude connect directly (no proxy), turn on the resource server. It
validates bearer JWTs against your IdP's JWKS. See
[Authentication & tenancy](../configuration/auth.md#native-oauth-resource-server).

```yaml
config:
  oauth:
    enabled: true
    issuer: https://keycloak.example.com/realms/memcove
    audience: memcove
    requiredScopes: ["memcove.use"]
    publicUrl: https://memcove.example.com   # your Ingress host; advertised as the resource id

ingress:
  enabled: true
  className: nginx
  hosts:
    - host: memcove.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: memcove-tls
      hosts: [memcove.example.com]
```

Without OAuth, keep it disabled and front Memcove with an authenticating proxy instead —
see the NetworkPolicy below and the [proxy recipe](../configuration/auth.md).

## What gets deployed

- **server** Deployment + Service (`:8090`), liveness on `/health`, readiness on `/ready`.
- **flight** Deployment + Service (`:8815`) — disable with `flight.enabled=false` if you
  don't use streaming. Set `flight.advertiseUri` to its cluster DNS so clients can dial it.
- **reconcile** CronJob (default `*/30 * * * *`) — heals registry/catalog drift; tune with
  `reconcile.schedule` or disable with `reconcile.enabled=false`.
- **ServiceAccount** (for IRSA), **ConfigMap** (non-secret `MEMCOVE_*`), and optionally an
  **Ingress** and **NetworkPolicy**.

Pods run non-root with a read-only root filesystem and all capabilities dropped.

## Lock down the network

If you use the proxy model (OAuth disabled), restrict ingress so **only** the proxy can
reach Memcove's ports — otherwise the tenant header is spoofable:

```yaml
networkPolicy:
  enabled: true
  proxyPodSelector:
    matchLabels:
      app: your-oidc-proxy
```

Restrict Trino the same way so per-tenant impersonation can't be sidestepped. See
[Kubernetes networking](kubernetes.md) and the [production checklist](checklist.md).

## Verify

```bash
kubectl rollout status deploy/memcove-server
kubectl port-forward svc/memcove-server 8090:8090 &
curl -fsS http://localhost:8090/ready
```

A `200` from `/ready` means the registry and Trino are both reachable. Then
[connect a client](../getting-started/connecting.md).
