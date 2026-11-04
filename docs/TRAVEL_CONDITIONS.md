# Travel conditions API

The existing guide service additionally exposes GET /v1/travel/health and POST /v1/travel/traffic. Existing guide/voice endpoints are retained. TOMTOM_API_KEY is a server-only SecretStr setting; TRAFFIC_DAILY_CALL_LIMIT defaults to 1000. Environment examples contain no credential values.

Traffic requests carry a route ID, CAR/MOTORCYCLE mode, and at most 2000 [longitude,latitude] vertices inside the Vietnam trial region. The service checks at most six positions, uses a 60-second cache, bounds upstream concurrency and rate, and omits low-confidence, offset or opposite-direction matches. Per-IP/body guards apply only to /v1/travel/ POST requests. These samples are general vehicle-flow estimates, not pedestrian counts or traffic-adjusted Valhalla ETA.

Missing configuration returns503; upstream failures return502 rather than a misleading empty/free-flow result. Partial coverage remains explicit. Cached observations retain their original retrieval timestamp. Provider URLs containing keys and raw routes are not logged. Public hosting still requires quota/abuse monitoring; the single-process counter resets on restart and is not a billing guarantee.

Validation: the full backend suite, including parent lifespan/health compatibility and provider-failure cases, passes104 tests. Ruff checks pass for the changed modules. No real API key or live traffic dataset is committed.
