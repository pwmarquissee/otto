# Dashboard polling cost (2026-10-02)

The dashboard polled GET /api/state every 2-3 s. Measured before this pass: 2,910,547 bytes per poll, no gzip offered, recomputed and reserialized on every request past a 1 s cache. Of that, `tasks` was 2.0 MB (834 tasks; `detail` alone 1.06 MB, 466 tasks over 600 chars) and `board.columns` 539 KB, which carries each card's `detail` a second time.

## What changed

- `Store.version` (otto/store.py): every state write (`_write`) and every event append bumps an int counter and stamps `version_at`. Telemetry appends do not, since nothing in the payload reads telemetry. Process-local, restarts at 0 with the daemon.
- `/ws/events` (otto/web_events.py): one WebSocket that announces version changes, polled every 250 ms on the loop. Goes through OriginGuard like every other socket.
- `/api/state` (otto/daemon.py): the payload is computed once per store version, serialized and gzipped once per fill, and served from cache until the store moves or `config.STATE_IDLE_CACHE_SECONDS` (10 s) passes. A write invalidates at once, so the poll after an action sees it. Strong `ETag` (sha1 of the JSON bytes), `Cache-Control: no-cache`; a matching `If-None-Match` gets a bodyless 304.
- `GZipMiddleware` (minimum_size 1024, level 6) covers every other JSON route; `/api/state` sends its own pre-compressed bytes.
- Task `detail` in `tasks[*]` and `board.columns[*].cards[*]` is cut to `config.STATE_DETAIL_CHARS` (600) with `detail_truncated: true` added only when something was cut. GET /api/tasks/{id} returns the full task, unflagged. Runs stay at 80 and events at 60 (`config.STATE_RUNS`, `config.STATE_EVENTS`): app.js renders every run it gets, grouped by day, and slices events at 60, so trimming either would remove visible history for under 60 KB.

## Numbers

The same payload pushed through the new route's own serialize-and-gzip path: 2,910,547 B before, 2,100,188 B plain after, 512,677 B on the wire with gzip (17.6% of today's bytes). Fill cost (serialize + gzip + sha1) 106 ms, paid once per version change. Sandbox with the 834 live tasks loaded: cold compute 866 ms, cached 200 with gzip 5.6 ms, 304 1.9 ms with 0 bytes.

## Events contract (do not change shapes, add fields only)

```
ws://127.0.0.1:8787/ws/events
server -> client: {"t":"hello","v":N}    on connect, current store.version
                  {"t":"changed","v":N}  the store moved, GET /api/state (send If-None-Match)
                  {"t":"ping"}           every 25 s while idle
client -> server: nothing; anything sent is read and ignored
```

`v` restarts at 0 with the daemon: compare against your own hello, never a remembered value.
