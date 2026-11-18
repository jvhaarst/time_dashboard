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
| `SERVER_LOCATION` | `Doorwerth, the Netherlands` | Shown in the subtitle. |
| `SERVER_HARDWARE` | a Pi with a Uputronics HAT | Shown in "The machine". |
| `POOL_URL` | empty | If set, the page says the server is an NTP Pool member and links here. |
| `LOG_LEVEL` | `INFO` | |

## What is on the page

| Section | Source |
|---|---|
| Live UTC clock and lock status | `chrony_tracking_*`, with `locked_since` walked back through the stratum series |
| Local offset check | the visitor's own clock against the server, measured over HTTP |
| Offset history, 1h/24h/7d/30d | `chrony_tracking_last_offset_seconds`, rolled up as mean with a min-max band |
| Summary per window | mean, standard deviation, peak excursion, sample count and locked fraction, all four windows in one query |
| Compared to other NTP servers | `chrony_sources_last_sample_offset_seconds` for the network sources this server polls |
| NTP sources table | `chrony_sources_*` and `chrony_sourcestats_*`, with chrony's own selection state |
| Lock calendar, 30 days | fraction of each day at `chrony_tracking_stratum == 1` |
| Allan deviation | `chrony_tracking_frequency_ppms`, overlapping ADEV over 30 days |
| Satellites | `gpsd_sat_ss` per satellite, plus DOP, mean C/No and leap seconds |
| What it serves | `chrony_serverstats_*` and `ntp_clients_active` per window |

Every chart is **inline SVG rendered on the server** — no charting library, no CDN, and
the page is still one request. The range switcher is four ordinary links, so it works
with JavaScript off and a range can be bookmarked. The only JavaScript is a ticking
clock and a poll of `/api/status` that refreshes a handful of live figures in place.

## Routes

| Route | Purpose |
|---|---|
| `/` | The page. `?range=1h\|24h\|7d\|30d` picks the history window. Returns **503** with a short explanation when the store is unreachable, rather than 200 with blanks, so a monitor can tell. |
| `/api/time` | The server's clock alone, uncached. The cheapest route on the site: its latency is the visitor's measurement error. |
| `/api/status` | The live figures as JSON. |
| `/api/history?range=24h` | Offset history for one window as JSON. |
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

**`source_mode` and `source_state` exist on `chrony_sources_state_info` only.** Every
other series in that family carries just `source_name` and `source_address`, so
filtering `chrony_sources_last_sample_offset_seconds{source_mode="client"}` matches
nothing, returns 200 with an empty result, and draws an empty chart with no error
anywhere. The network sources have to be named from `state_info` first.

## Four traps in MetricsQL

All four cost a wrong page rather than an exception, except where noted.

**A subquery inherits the outer step unless you give it one.** `(expr)[1d]` where expr
is not a bare selector is a subquery. In a range query stepping by a day,
`sum_over_time((stratum == bool 1)[1d])` evaluated the inner expression *once* per day
while `count_over_time(stratum[1d])` counted 2,880 raw samples, so availability came out
as 1/2880 and the lock calendar drew 31 consecutive red days on a server that had not
missed a second. The fix is the explicit step in `[1d:300]`.

**`metric_a or metric_b` drops `__name__` before matching labels**, so a union of
single-series metrics that differ only in their name silently returns just the first.
That is why the instant query is one regex selector instead of a chain of `or`.

**A range query that would exceed `-search.maxPointsPerTimeseries` is refused**, whole,
with HTTP 422 — it is not truncated. 30 days at a 60 s step is 43,200 points and fails;
120 s is 21,600 and works. That limit, not taste, is why the Allan deviation's shortest
tau is 120 s.

**Label regexes need two layers of escaping, and each fails differently.** The inner
layer is RE2: a literal `.` must become `\.`. The outer layer is MetricsQL's string
literal, which is stricter than Prometheus's and rejects `"\."` with *cannot parse
string literal ... invalid syntax*, so the backslash has to be doubled in the query
text. And `re.escape` cannot produce the inner layer: Python escapes `-` as `\-`, which
RE2 rejects as an invalid escape sequence — relevant here because one of this server's
sources is `static.ip-031-223-173-226.signet.nl`.

## The local offset check

The page measures the visitor's clock against the server and corrects the big digits by
the result, so what is displayed is the **server's** time rather than the browser's.

It is NTP's own arithmetic over HTTP:

    offset = t1 - (t0 + t3) / 2          delay = t3 - t0

`t0` is taken just before the request, `t3` just after the response headers arrive, and
`t1` is `/api/time`'s timestamp. One server timestamp stands for both the receive and
transmit times: the handler does nothing in between, so the two differ by microseconds
while the network contributes milliseconds, and returning both would imply a precision
that is not there.

Five samples are taken and **the one with the shortest round trip wins** — the same
choice chrony and ntpd make. A long round trip is nearly always one-sided queueing, and
an asymmetric path biases the offset by half the asymmetry, so the best sample is the
least biased one rather than the average. The round trip is quoted alongside the result
because it bounds the error: the true offset is within half of it.

This is good to a few milliseconds over a LAN and tens over the internet. It is a
sanity check, not a measurement of the server, and the page says so.

## One Flask trap

A context variable shadows a Jinja global of the same name. The template receives
`offset` (the current offset, as text) and also needs a formatting helper; registering
that helper as `offset` makes every call fail with `'str' object is not callable`. It is
registered as `fmt_offset`.

## Versioning

`VERSION` holds the major.minor; CI appends the run number, so the published tag is
`<VERSION>.<run>`. The chart's `appVersion` tracks that tag and Renovate bumps it.
