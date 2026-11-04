"""Travel traffic proxy. Provider credentials stay server-side; no raw route/access logs."""

import asyncio
import logging
import math
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)


def xy(point, anchor):
    return (
        (point[0] - anchor[0]) * math.cos(math.radians(anchor[1])) * 111195,
        (point[1] - anchor[1]) * 111195,
    )


def distance(a, b):
    return math.hypot(*xy(a, b))


def projection(point, a, b):
    x, y = xy(b, a)
    px, py = xy(point, a)
    fraction = max(0, min(1, (px * x + py * y) / (x * x + y * y))) if x * x + y * y > 0.001 else 0
    return math.hypot(px - fraction * x, py - fraction * y), math.degrees(math.atan2(x, y)) % 360


class RouteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-zA-Z0-9_-]+$")
    mode: Literal["MOTORCYCLE", "CAR"]
    coordinates: list[tuple[float, float]] = Field(min_length=2, max_length=2000)

    @field_validator("coordinates")
    @classmethod
    def validate_coordinates(cls, points):
        if not all(
            math.isfinite(lon) and math.isfinite(lat) and 102 <= lon <= 110 and 8 <= lat <= 24
            for lon, lat in points
        ):
            raise ValueError("Route outside supported Vietnam trial area")
        length = sum(distance(a, b) for a, b in zip(points, points[1:]))
        if not 0 < length <= 100000:
            raise ValueError("Invalid route length")
        return points


def sample_route(points, maximum=6):
    segments = [(a, b, distance(a, b)) for a, b in zip(points, points[1:]) if distance(a, b) > 0.2]
    total = sum(length for _, _, length in segments)
    if not segments:
        return []
    count = min(maximum, max(2, math.ceil(total / 250)))
    result = []
    for index in range(count):
        along = total * ((index + 0.5) / count)
        remaining = along
        for a, b, length in segments:
            if remaining <= length:
                f = remaining / length
                point = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
                result.append((point, along, projection(point, a, b)[1]))
                break
            remaining -= length
    return result


def match_flow(flow, sample, route):
    point, along, heading = sample
    confidence = float(flow.get("confidence", 0))
    if not math.isfinite(confidence) or not 0.7 <= confidence <= 1:
        return None
    raw_points = flow.get("coordinates", {}).get("coordinate", [])
    if not 2 <= len(raw_points) <= 2000:
        return None
    points = [(p["longitude"], p["latitude"]) for p in raw_points]
    if not all(
        math.isfinite(x) and math.isfinite(y) and -180 <= x <= 180 and -90 <= y <= 90
        for x, y in points
    ):
        return None
    nearby = [
        (projection(point, a, b), a, b) for a, b in zip(points, points[1:]) if distance(a, b) > 1
    ]
    if not nearby:
        return None
    (offset, bearing), a, b = min(nearby, key=lambda row: row[0][0])
    difference = abs((bearing - heading + 180) % 360 - 180)
    # Direction and centreline checks deliberately reject uncertain parallel/opposite roads.
    if offset > 12 or difference > 40:
        return None
    for candidate in (a, b):
        if (
            distance(candidate, point) < 50
            and min(projection(candidate, x, y)[0] for x, y in zip(route, route[1:])) > 15
        ):
            return None
    current = float(flow["currentTravelTime"])
    free = float(flow["freeFlowTravelTime"])
    if not math.isfinite(current) or not math.isfinite(free) or current < 0 or free <= 0:
        return None
    return {
        "lat": point[1],
        "lon": point[0],
        "along_meters": round(along, 2),
        "current_seconds": current,
        "free_flow_seconds": free,
        "confidence": confidence,
        "closed": flow.get("roadClosure") is True,
    }


class TrafficProvider:
    def __init__(self, client, key, daily_limit=1000):
        self.client, self.key, self.daily_limit = client, key, daily_limit
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(3)
        self.calls = 0
        self.day = int(time.time() // 86400)
        self.last_call = 0.0

    async def get(self, point, heading):
        identity = (round(point[0], 5), round(point[1], 5), round(heading / 15))
        now = time.time()
        async with self.lock:
            cached = self.cache.get(identity)
            if cached and now - cached[0] < 60:
                return cached[1], cached[0]
        async with self.slots:
            async with self.lock:
                day = int(time.time() // 86400)
                if day != self.day:
                    self.day, self.calls = day, 0
                if self.calls >= self.daily_limit:
                    return None, time.time()
                await asyncio.sleep(max(0, 0.2 - (time.monotonic() - self.last_call)))
                self.last_call = time.monotonic()
                self.calls += 1
            try:
                response = await self.client.get(
                    "https://api.tomtom.com/traffic/services/4/flowSegmentData/relative0/18/json",
                    params={
                        "key": self.key,
                        "point": f"{point[1]:.6f},{point[0]:.6f}",
                        "unit": "kmph",
                        "thickness": 1,
                    },
                )
                value = (
                    response.json().get("flowSegmentData") if response.status_code == 200 else None
                )
            except (httpx.HTTPError, ValueError):
                value = None
            async with self.lock:
                fetched = time.time()
                self.cache[identity] = (fetched, value)
                while len(self.cache) > 512:
                    self.cache.popitem(last=False)
            return value, fetched


class TravelRequestGuard(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.last_by_ip = OrderedDict()

    async def dispatch(self, request, call_next):
        if request.method == "POST" and request.url.path.startswith("/v1/travel/"):
            length = request.headers.get("content-length", "")
            if (
                not length.isdigit()
                or not 0 < int(length) <= 262144
                or request.headers.get("transfer-encoding")
            ):
                return JSONResponse({"status": "invalid_request"}, status_code=413)
            ip = request.client.host if request.client else "unknown"
            now = time.monotonic()
            if now - self.last_by_ip.get(ip, -100) < 5:
                return JSONResponse({"status": "rate_limited"}, status_code=429)
            self.last_by_ip[ip] = now
            while len(self.last_by_ip) > 2048:
                self.last_by_ip.popitem(last=False)
        return await call_next(request)


def create_app(key=None, transport=None, daily_limit=None):
    key = key if key is not None else os.getenv("TOMTOM_API_KEY", "").strip()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            timeout=5.5, follow_redirects=False, transport=transport
        ) as client:
            app.state.travel_traffic_provider = TrafficProvider(
                client,
                key,
                daily_limit
                if daily_limit is not None
                else int(os.getenv("TRAFFIC_DAILY_CALL_LIMIT", "1000")),
            )
            yield

    app = FastAPI(
        title="Veilwake Travel Conditions", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.add_middleware(TravelRequestGuard)

    @app.get("/v1/travel/health")
    async def health():
        return {"status": "ok", "traffic_configured": bool(key)}

    @app.post("/v1/travel/traffic")
    async def traffic(payload: RouteInput, request: Request):
        if not key:
            raise HTTPException(503, "Traffic provider not configured")
        samples = sample_route(payload.coordinates)
        values = await asyncio.gather(
            *(
                request.app.state.travel_traffic_provider.get(point, heading)
                for point, _, heading in samples
            )
        )
        if not any(value for value, _ in values):
            raise HTTPException(502, "Traffic provider temporarily unavailable")
        accepted = []
        timestamps = []
        for sample, (value, fetched) in zip(samples, values):
            if value:
                try:
                    result = match_flow(value, sample, payload.coordinates)
                    if result:
                        accepted.append(result)
                        timestamps.append(fetched)
                except (ValueError, TypeError, KeyError):
                    pass
        return {
            "route_id": payload.route_id,
            "fetched_at": int(min(timestamps) if timestamps else time.time()),
            "source": "TomTom Traffic",
            "samples_total": len(samples),
            "samples": accepted,
        }

    return app


app = create_app()
