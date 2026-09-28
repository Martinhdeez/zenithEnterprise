"""Small concurrent real-route tenant isolation check; no scheduling fairness claim."""

import concurrent.futures
import json
import statistics
import time
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parents[2] / ".scratch/release-validation"
fixture = json.loads((OUT / "fixture.json").read_text(encoding="utf-8"))


def main() -> None:
    origin = fixture["base_url"]
    credentials = {}
    with httpx.Client(base_url=origin, timeout=30) as client:
        for identity in ("alpha-member", "beta-member"):
            response = client.post("/auth/login", json=fixture["users"][identity])
            response.raise_for_status()
            credentials[identity] = response.json()["access_token"]

    def search(identity: str, query: str) -> dict[str, object]:
        started = time.perf_counter()
        with httpx.Client(base_url=origin, timeout=40) as client:
            response = client.get(
                "/search",
                params={"q": query, "mode": "hybrid"},
                headers={"Authorization": f"Bearer {credentials[identity]}"},
            )
        response.raise_for_status()
        body = response.json()
        hits = body["hits"]
        if identity == "alpha-member":
            assert all(
                hit["document_id"] != fixture["documents"]["tenant-hidden"]["id"] for hit in hits
            )
            assert all(
                hit["document_id"] != fixture["documents"]["label-hidden"]["id"] for hit in hits
            )
        else:
            assert all(
                hit["document_id"]
                not in {
                    fixture["documents"]["text"]["id"],
                    fixture["documents"]["pdf"]["id"],
                    fixture["documents"]["label-hidden"]["id"],
                }
                for hit in hits
            )
        return {
            "identity": identity,
            "status": response.status_code,
            "hits": len(hits),
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "degraded": body["degraded"],
        }

    requests = [
        ("alpha-member", "When does the cobalt telescope open?"),
        ("alpha-member", "Hiddenlabelcanary lantern reserve"),
        ("alpha-member", "Hiddentenantcanary orchid reserve"),
        ("beta-member", "Hiddentenantcanary orchid reserve"),
        ("beta-member", "When does the cobalt telescope open?"),
        ("beta-member", "Hiddenlabelcanary lantern reserve"),
    ] * 2
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(lambda item: search(*item), requests))
    assert len(outcomes) == 12
    report = {
        "fixture_head": fixture["head"],
        "requests": len(outcomes),
        "max_workers": 6,
        "all_200": all(item["status"] == 200 for item in outcomes),
        "degraded": sum(bool(item["degraded"]) for item in outcomes),
        "latency_p50_ms": statistics.median(int(item["elapsed_ms"]) for item in outcomes),
        "latency_max_ms": max(int(item["elapsed_ms"]) for item in outcomes),
        "isolation_assertions": 12,
    }
    (OUT / "load-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
