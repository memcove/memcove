# Run with Docker

Memcove publishes a single multi-arch (amd64 + arm64) image containing all three entry
points. Use it to run the service without a local Python setup — for a container
platform, a compose file, or a quick trial against your own infrastructure.

## The image

| Registry | Reference |
| --- | --- |
| Docker Hub | `andrzejgluszynski/memcove` |
| GHCR | `ghcr.io/memcove/memcove` |

Tags track releases (e.g. `:0.9.0`) plus `:latest`. Pull either mirror:

```bash
docker pull ghcr.io/memcove/memcove:latest
```

## Entry points

One image, three commands — pick with the container's command:

| Command | Role | Port |
| --- | --- | --- |
| `memcove-server` (default) | MCP control plane, Streamable HTTP | 8090 |
| `memcove-flight` | Arrow Flight data plane, gRPC (only for [streaming](../tools/streaming.md)) | 8815 |
| `memcove-reconcile` | one-shot registry/catalog drift repair (run on a schedule) | — |

## Configure

Every setting is a `MEMCOVE_*` environment variable (see the
[settings reference](../configuration/settings.md)). The simplest path is an env file —
start from the shipped example:

```bash
cp .env.example memcove.env
# edit memcove.env: point S3 / Iceberg / Trino / registry at your infra
```

At minimum, a real deployment sets the object store, Iceberg REST URI, Trino host, the
registry DSN (`MEMCOVE_REGISTRY_DSN`), and a strong `MEMCOVE_FLIGHT_TICKET_SECRET`. On
AWS, leave `MEMCOVE_S3_ACCESS_KEY`/`MEMCOVE_S3_SECRET_KEY` empty to use the instance/pod
IAM role.

## Run the server

```bash
docker run --rm -p 8090:8090 --env-file memcove.env \
  ghcr.io/memcove/memcove:latest
```

The server initializes the registry on startup and serves MCP at
`http://localhost:8090/mcp`. Health probes are built in:

```bash
curl -fsS http://localhost:8090/health   # liveness — always 200 when the process is up
curl -fsS http://localhost:8090/ready    # readiness — 200 only when registry + Trino are reachable
```

## Run the Flight data plane

Only needed for the [streaming tools](../tools/streaming.md). It must advertise a URI
clients can dial, so set `MEMCOVE_FLIGHT_ADVERTISE_URI` to its reachable address:

```bash
docker run --rm -p 8815:8815 --env-file memcove.env \
  -e MEMCOVE_FLIGHT_ADVERTISE_URI=grpc://your-host:8815 \
  ghcr.io/memcove/memcove:latest memcove-flight
```

## Run the reconciler

A batch job — run it on a schedule (cron, a Kubernetes CronJob, etc.):

```bash
docker run --rm --env-file memcove.env \
  ghcr.io/memcove/memcove:latest memcove-reconcile
```

!!! warning "Don't expose Memcove directly"
    In the default header model, anything that can reach port 8090 can set the tenant
    header and read that tenant's data. Put an authenticating proxy in front and restrict
    the network, or enable [native OAuth](../configuration/auth.md#native-oauth-resource-server).
    See the [production checklist](../deployment/checklist.md).

## Next

- Full local stack (MinIO + Iceberg + Trino + Postgres) for development:
  [Quickstart](quickstart.md).
- Cluster install with probes, secrets, and IRSA: [Install with Helm](../deployment/helm.md).
- Point an agent at it: [Connect an MCP client](connecting.md).
