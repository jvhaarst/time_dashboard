# time_dashboard

The public status page at **<https://time.vanhaarst.net>** for `ntp.vanhaarst.net`, a
GPS-disciplined stratum-1 NTP server.

It renders server-side from a VictoriaMetrics store and ships as
`ghcr.io/jvhaarst/time_dashboard`. The Helm chart that deploys it lives in
[time_dashboard_k8s](https://github.com/jvhaarst/time_dashboard_k8s).

## How it gets its numbers

```
chrony-exporter ─┐
gpsd-exporter   ─┼─ scraped by vmagent ─┬─ main store, 30d, everything
clients-exporter┘                       └─ VMSingle/ntp, 1y, these series only
node-exporter   ─┘                               │
                                                  ▼
                                      this app queries it in-cluster
                                                  │
                                                  ▼
                                      rendered HTML to the visitor
```

The store is `vmsingle-ntp.monitoring.svc.cluster.local:8429`, a ClusterIP with no route
from outside the cluster. **The browser never queries it.** That is the point of
rendering server-side: a public page backed by a metrics store would otherwise mean
either exposing arbitrary PromQL to the internet or putting a query proxy in front of
it. Here the only thing that leaves the cluster is the finished page.

It also means the page works with JavaScript switched off, and that a visitor cannot
make the store do work: a rendered page is reused for `CACHE_SECONDS` (15 by default),
and the exporters only scrape every 30 s, so a shorter cache would add load without
adding information.

One HTTP request to VictoriaMetrics fetches every instant value, using a regex over
`__name__`. A second fetches the request rate, which needs `rate()` over a counter.

## Configuration

All environment variables, all optional.

| Variable | Default | What it does |
|---|---|---|
| `VM_URL` | `http://vmsingle-ntp.monitoring.svc.cluster.local:8429` | The store to query. 8429 is a vmsingle's real listener; its Service also publishes 8428 as an alias. |
| `VM_TIMEOUT` | `5` | Seconds per query. Keep it below the readiness probe's timeout, so a slow store makes the page late rather than the pod unready. |
| `CACHE_SECONDS` | `15` | How long a rendered page is reused. |
| `SERVER_NAME` | `ntp.vanhaarst.net` | The hostname the page tells clients to use. |
| `SERVER_LOCATION` | `Wageningen, the Netherlands` | Shown in the subtitle. |
| `SERVER_HARDWARE` | a Pi with a Uputronics HAT | Shown in "The machine". |
| `POOL_URL` | empty | If set, the page says the server is an NTP Pool member and links here. |
| `LOG_LEVEL` | `INFO` | |

## Routes

| Route | Purpose |
|---|---|
| `/` | The page. Returns **503** with a short explanation when the store is unreachable, rather than 200 with blanks, so a monitor can tell. |
| `/healthz` | Liveness. Deliberately does **not** query VictoriaMetrics. |
| `/readyz` | Readiness. Does query it. |

Splitting those two is not decoration. A liveness probe that depends on another service
restarts a healthy pod whenever that service is slow; on this cluster that exact mistake
restarted the `zeildashboard` pod five times in nine minutes when a third-party API
stopped answering, while the site itself was serving fine.

## Development

```bash
uv sync
uv run pytest -q

# The store is in-cluster, so forward it first.
ssh -f -N -L 18429:"$(kubectl -n monitoring get svc vmsingle-ntp -o jsonpath='{.spec.clusterIP}')":8429 raspi5.local
VM_URL=http://127.0.0.1:18429 CACHE_SECONDS=0 uv run flask --app app.main run --port 8080
```

## Two traps in the source metrics

Both of these produce a page that looks right and is wrong, so they are the two things
the tests cover.

**`gpsd_sat_used` is not per-satellite.** Despite the name it is a single gauge holding
the number of satellites in the fix, with no `gnssid` label, so
`sum by (gnssid) (gpsd_sat_used)` buckets everything under an empty key and every
per-constellation figure renders as 0 while the total stays correct. The per-satellite
flag is the `used` label on `gpsd_sat_ss`, written by the exporter as Python's
`str(bool)` — so the values are the strings `"True"` and `"False"`, not `1` and `0`.

**`ntp_clients_log_entries` is not a client count.** chronyd's client log keeps every
address it has ever answered until `clientloglimit` evicts the oldest, so it measures
the log, not demand — on this server the two differ by two orders of magnitude. The page
shows `ntp_clients_active` per window for demand and labels the log as a saturation
indicator.

A third one is about PromQL rather than the metrics: `metric_a or metric_b` compares
label sets **without** `__name__`, so a union of several single-series metrics that
differ only in their name silently returns just the first. That is why the instant query
is one regex selector instead of a chain of `or`.

## Versioning

`VERSION` holds the major.minor; CI appends the run number, so the published tag is
`<VERSION>.<run>`. The chart's `appVersion` tracks that tag and Renovate bumps it.
