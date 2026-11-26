# Log type per scenario (decided from dev logs, before the v0.2 freeze)
# 1 = app log names the wrong setting or its wrong value
# 2 = app logs errors, but nothing points to the setting
# 3 = app logs no warnings/errors about it
# Only orders and frontend logs count. Loadgen and Kubernetes events are ignored.
# Structured log fields (e.g. redis_port) count as part of the log line.

| Scenario | What the app logs (dev) | Type | Reason |
|---|---|---|---|
| F01-a | frontend: connection refused to orders:5000 | 2 | errors, but nothing says targetPort |
| F02-a | orders: cannot resolve redis-cache | 1 | the wrong value redis-cache is in the log |
| F02-b | frontend: cannot resolve 'order' | 1 | the wrong value 'order' is in the log |
| F03-a | orders: password authentication failed, then exits | 2 | says what kind of problem, but not DB_PASSWORD or the wrong value |
| F04-a | only frontend: connection refused to orders | 2 | orders is OOMKilled and logs nothing; frontend errors don't mention memory |
| F04-b | nothing | 3 | frontend is OOMKilled and logs nothing |
| F05-a | only frontend: connection refused to orders | 2 | nothing mentions the readiness path |
| F05-b | nothing | 3 | frontend is never ready and nobody logs an error |
| F06-a | only frontend: connection refused to orders (fewer) | 2 | nothing mentions the liveness port |
| F07-a | only frontend: connection refused to orders | 2 | orders never starts; nothing mentions the image |
| F07-b | nothing | 3 | frontend never starts and nobody logs an error |
| F08-a | only frontend: connection refused to orders | 2 | nothing mentions the Service selector |
| F08-b | nothing | 3 | nobody logs an error |
| F09-a | orders: connection to postgres refused; frontend errors | 2 | errors, but nothing names the NetworkPolicy |
| F10-a | only frontend: connection errors / timeouts to orders | 2 | nothing mentions CPU |
| F10-b | nothing | 3 | frontend is CPU-starved and logs nothing |
| F11-a | orders: "Timeout connecting to server", redis_port 6380 in a field | 1 | the wrong port is in the log line, but only as a field (message text alone would be type 2) |
| F11-b | no dev runs; expected: frontend error with orders:5001 | 1 | expected: the wrong port 5001 in the message (check in test data) |
