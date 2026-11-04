import asyncio
import unittest

import httpx
from fastapi.testclient import TestClient

from app.api.travel_conditions import TrafficProvider, create_app, match_flow, sample_route

ROUTE = [(106.7, 10.77), (106.702, 10.77)]


def flow(points=ROUTE, confidence=0.98):
    return {
        "confidence": confidence,
        "currentTravelTime": 100,
        "freeFlowTravelTime": 40,
        "roadClosure": False,
        "coordinates": {"coordinate": [{"longitude": x, "latitude": y} for x, y in points]},
    }


class TrafficProxyTest(unittest.TestCase):
    def test_same_road_and_direction_accepts_but_parallel_opposite_and_low_confidence_do_not(self):
        sample = sample_route(ROUTE)[0]
        self.assertIsNotNone(match_flow(flow(), sample, ROUTE))
        self.assertIsNone(match_flow(flow(list(reversed(ROUTE))), sample, ROUTE))
        self.assertIsNone(match_flow(flow([(x, y + 0.0002) for x, y in ROUTE]), sample, ROUTE))
        self.assertIsNone(match_flow(flow(confidence=0.2), sample, ROUTE))

    def test_samples_are_distance_ordered_and_bounded_even_with_many_vertices(self):
        points = [(106.7 + i * 0.00001, 10.77) for i in range(1900)]
        samples = sample_route(points)
        self.assertEqual(6, len(samples))
        self.assertTrue(all(a[1] < b[1] for a, b in zip(samples, samples[1:])))

    def test_no_key_is_explicitly_unavailable_and_health_never_exposes_credentials(self):
        with TestClient(create_app(key="")) as client:
            self.assertEqual(
                {"status": "ok", "traffic_configured": False},
                client.get("/v1/travel/health").json(),
            )
            response = client.post(
                "/v1/travel/traffic", json={"route_id": "r", "mode": "CAR", "coordinates": ROUTE}
            )
            self.assertEqual(503, response.status_code)

    def test_real_contract_and_invalid_scope_cannot_be_relabelled_as_traffic(self):
        calls = []

        def mock(request):
            calls.append(request)
            return httpx.Response(200, json={"flowSegmentData": flow()})

        with TestClient(
            create_app(key="unit-test-secret", transport=httpx.MockTransport(mock))
        ) as client:
            response = client.post(
                "/v1/travel/traffic",
                json={"route_id": "route-123", "mode": "CAR", "coordinates": ROUTE},
            )
            self.assertEqual(200, response.status_code)
            value = response.json()
            self.assertEqual("route-123", value["route_id"])
            self.assertTrue(value["samples"])
            self.assertNotIn("unit-test-secret", response.text)
            self.assertEqual("kmph", calls[0].url.params["unit"])
        with TestClient(
            create_app(key="unit-test-secret", transport=httpx.MockTransport(mock))
        ) as client:
            response = client.post(
                "/v1/travel/traffic", json={"route_id": "r", "mode": "WALK", "coordinates": ROUTE}
            )
            self.assertEqual(422, response.status_code)

    def test_parent_guide_app_lifespan_and_health_remain_compatible(self):
        from app.core.config import Settings
        from app.main import create_app as create_guide_app

        settings = Settings(_env_file=None, tomtom_api_key=None)
        with TestClient(create_guide_app(settings)) as client:
            self.assertEqual(200, client.get("/health").status_code)
            self.assertFalse(client.get("/v1/travel/health").json()["traffic_configured"])
            response = client.post(
                "/v1/travel/traffic", json={"route_id": "r", "mode": "CAR", "coordinates": ROUTE}
            )
            self.assertEqual(503, response.status_code)

    def test_provider_failure_is_not_returned_as_a_free_flowing_route(self):
        def mock(request):
            return httpx.Response(429, json={"error": "quota"})

        with TestClient(
            create_app(key="unit-test-secret", transport=httpx.MockTransport(mock))
        ) as client:
            response = client.post(
                "/v1/travel/traffic", json={"route_id": "r", "mode": "CAR", "coordinates": ROUTE}
            )
            self.assertEqual(502, response.status_code)
            self.assertNotIn("unit-test-secret", response.text)

    def test_cached_timestamp_is_not_falsely_refreshed_and_budget_is_bounded(self):
        async def run():
            calls = []

            def mock(request):
                calls.append(1)
                return httpx.Response(200, json={"flowSegmentData": flow()})

            async with httpx.AsyncClient(transport=httpx.MockTransport(mock)) as client:
                provider = TrafficProvider(client, "unit-test-secret", daily_limit=1)
                first = await provider.get(ROUTE[0], 90)
                cached = await provider.get(ROUTE[0], 90)
                self.assertEqual(first[1], cached[1])
                self.assertEqual(1, len(calls))
                over = await provider.get(ROUTE[1], 90)
                self.assertIsNone(over[0])
                self.assertEqual(1, len(calls))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
