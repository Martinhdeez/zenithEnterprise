"""Default-off behavior checks; only generation uses the shipped mock model boundary."""

import json
import time
from urllib.parse import urlsplit

from browser import OUT, fixture, login, search
from playwright.sync_api import expect, sync_playwright


def authorize(page):
    # This is the genuine server-issued UI session, never a fabricated token.
    return {"Authorization": "Bearer " + page.evaluate("sessionStorage.getItem('zenith.token')")}


def upload(page, origin, name, text):
    authenticated = page.request.post(origin + "/auth/login", data=fixture["users"]["alpha-admin"])
    assert authenticated.ok
    headers = {"Authorization": "Bearer " + authenticated.json()["access_token"]}
    response = page.request.post(
        origin + "/documents",
        headers=headers,
        multipart={
            "file": {"name": name, "mimeType": "text/plain", "buffer": text.encode()},
            "labels": fixture["labels"]["alpha"]["default"],
        },
    )
    assert response.status == 201, response.text()
    doc = response.json()["document"]
    assert doc["status"] == "pending"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        response = page.request.get(origin + f"/documents/{doc['id']}", headers=headers)
        assert response.ok
        if response.json()["status"] == "ready":
            return doc
        assert response.json()["status"] != "failed", response.json()
        page.wait_for_timeout(500)
    raise AssertionError("worker did not finish ingestion")


def ask(page, question, *, followup=False):
    # Opening the conversation submits the search question automatically.
    # Capture that response instead of racing it with a duplicate submission.
    with page.expect_response(lambda r: r.url.endswith("/query/stream"), timeout=60000) as response:
        page.get_by_role("button", name="Ask about this document", exact=True).click()
    if followup:
        response.value.text()
        field = page.get_by_role("textbox", name="Question", exact=True)
        field.fill(question)
        with page.expect_response(
            lambda r: r.url.endswith("/query/stream"), timeout=60000
        ) as response:
            field.press("Enter")
    assert response.value.status == 200
    stream = response.value.text()
    result = None
    for block in stream.replace("\r\n", "\n").split("\n\n"):
        if "event: result" in block:
            result = json.loads(
                next(line[6:] for line in block.splitlines() if line.startswith("data: "))
            )
    assert result is not None, stream
    expect(page.get_by_text("Searching your documents")).to_have_count(0, timeout=30000)
    return result, stream


def assert_buffered(result, stream):
    tokens = []
    for block in stream.replace("\r\n", "\n").split("\n\n"):
        if "event: token" in block:
            tokens.append(
                "\n".join(line[6:] for line in block.splitlines() if line.startswith("data: "))
            )
    assert tokens == [result["answer"]], tokens


def main():
    evidence, events = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")

        def watch(tab):
            tab.on(
                "pageerror",
                lambda error: events.append(
                    {"origin": urlsplit(tab.url).netloc, "pageerror": str(error)}
                ),
            )
            tab.on(
                "console",
                lambda message: (
                    events.append(
                        {
                            "origin": urlsplit(tab.url).netloc,
                            "console": message.type,
                            "text": message.text,
                        }
                    )
                    if message.type == "error"
                    else None
                ),
            )
            tab.on(
                "response",
                lambda response: (
                    events.append(
                        {
                            "origin": urlsplit(response.url).netloc,
                            "status": response.status,
                            "path": urlsplit(response.url).path,
                        }
                    )
                    if response.status >= 400
                    else None
                ),
            )

        context.on("page", watch)
        page = context.new_page()
        try:
            origin = fixture["experimental_url"]
            page.goto(origin, wait_until="domcontentloaded")
            login(page, "alpha-member")
            expect(page.locator('#search-mode option[value="direct"]')).to_have_count(1)
            page.locator("#search-mode").select_option("direct")
            body = search(page, "What do saffron submarines pay?")
            receipt = body["receipt"]
            assert receipt["strategy"] == "direct" and receipt["manifest_assessment_complete"], body
            assert receipt["eligible_units"] == receipt["assessed_units"] == 4, receipt
            expect(
                page.get_by_text("Every eligible parsed passage in this scope was assessed.")
            ).to_be_visible()
            evidence.append({"direct_at_cap": receipt})
            page.screenshot(path=str(OUT / "direct-complete.png"))

            # Supported ingestion adds cap+one, never a ready-row shortcut.
            upload(
                page,
                origin,
                "acceptance-overflow.txt",
                "Overflow scope fixture. Additional harbor registration guidance applies "
                "to all vessels. "
                "This extra searchable passage exceeds the disposable direct cap.",
            )
            body = search(page, "What do saffron submarines pay?")
            assert not body["receipt"]["manifest_assessment_complete"]
            expect(page.get_by_text("The selected scope was not fully assessed.")).to_be_visible()
            evidence.append({"direct_overflow": body["receipt"]})
            page.locator("#search-mode").select_option("auto")
            body = search(page, "What do saffron submarines pay?")
            assert body["receipt"]["strategy"] == "hybrid"
            assert not body["receipt"]["manifest_assessment_complete"]
            evidence.append({"auto_overflow": body["receipt"]})
            page.screenshot(path=str(OUT / "auto-fallback.png"))

            # Real source-scoped generation with packets, model boundary scripted only.
            page.locator("#search-mode").select_option("hybrid")
            body = search(page, "When does the cobalt telescope open?")
            page.get_by_role("button").filter(has_text="acceptance-observatory.txt").first.click()
            result, stream = ask(page, "When does the cobalt telescope open?")
            assert not result["abstained"] and result["citations"], result
            assert result["citations"][0]["document_id"] == fixture["documents"]["text"]["id"]
            assert_buffered(result, stream)
            evidence.append({"packet_original_identity": result})
            page.screenshot(path=str(OUT / "packet-source-answer.png"))
            private_question_id = result["query_id"]

            # A separate strict instance shares real RLS/auth but has no permitted assessor.
            strict = context.new_page()
            strict.goto(fixture["strict_url"], wait_until="domcontentloaded")
            login(strict, "alpha-member")
            strict.locator("#search-mode").select_option("hybrid")
            search(strict, "When does the cobalt telescope open?")
            strict.get_by_role("button").filter(has_text="acceptance-observatory.txt").first.click()
            result, stream = ask(strict, "When does the cobalt telescope open?")
            assert result["abstained"] and result["support_status"] == "not_assessed", result
            assert result["reason"] == "support_unavailable" and not result["citations"], result
            assert_buffered(result, stream)
            assert "This is a mock answer" not in stream
            expect(strict.get_by_text("A supported answer could not be verified.")).to_be_visible()
            strict.screenshot(path=str(OUT / "strict-buffered-abstention.png"))
            evidence.append({"strict_buffered": result})

            # An unavailable local reranker must yield incomplete direct and honest fallback.
            unavailable = context.new_page()
            unavailable.goto(fixture["unavailable_url"], wait_until="domcontentloaded")
            login(unavailable, "alpha-member")
            body = search(unavailable, "When does the cobalt telescope open?")
            assert body["degraded"], body
            evidence.append({"legacy_unavailable": body["relevance"]})
            unavailable.locator("#search-mode").select_option("direct")
            body = search(unavailable, "When does the cobalt telescope open?")
            assert body["degraded"] and not body["receipt"]["manifest_assessment_complete"], body
            expect(
                unavailable.get_by_text("The selected scope was not fully assessed.")
            ).to_be_visible()
            evidence.append({"direct_unavailable": body["receipt"]})
            unavailable.locator("#search-mode").select_option("auto")
            body = search(unavailable, "When does the cobalt telescope open?")
            assert body["degraded"] and body["receipt"]["strategy"] == "hybrid", body
            assert not body["receipt"]["manifest_assessment_complete"]
            evidence.append({"auto_unavailable": body["receipt"]})
            unavailable.screenshot(path=str(OUT / "dependency-unavailable.png"))

            # Partial packet state from an actual ingested unresolved section reference.
            partial = upload(
                page,
                origin,
                "acceptance-unresolved.txt",
                "Residents pay the harbor tariff subject to section 7. "
                "The required harbor tariff exception is defined in that section. "
                "Residents must retain their tariff receipt.",
            )
            page.get_by_role("button", name="Search", exact=True).first.click()
            search(page, "Residents harbor tariff section 7")
            page.get_by_role("button").filter(has_text="acceptance-unresolved.txt").first.click()
            result, stream = ask(page, "What harbor tariff do residents pay?", followup=True)
            assert result["abstained"] and result["reason"] == "packet_dependencies_unresolved", (
                result
            )
            assert_buffered(result, stream)
            expect(
                page.get_by_text("Some required source context was unavailable.").last
            ).to_be_visible()
            evidence.append({"packet_unresolved": result, "fixture_document": partial["id"]})
            page.screenshot(path=str(OUT / "packet-unresolved.png"))

            # Same-tenant user history remains private even with shared document access.
            page.get_by_role("button", name="Profile", exact=True).click()
            page.get_by_role("button", name="Sign out", exact=True).click()
            login(page, "alpha-admin")
            response = page.request.get(
                origin + "/query/history?mine=true", headers=authorize(page)
            )
            assert response.ok and private_question_id not in response.text()
            evidence.append({"same_tenant_own_history_private": True})
            assert not events, events
            print(
                f"PASS: {len(evidence)} experimental browser checks "
                "(scripted generation, real auth/RLS/TEI)"
            )
        except Exception:
            page.screenshot(path=str(OUT / "experimental-failure.png"))
            (OUT / "experimental-failure-dom.txt").write_text(
                page.locator("body").inner_text(), encoding="utf-8"
            )
            raise
        finally:
            (OUT / "experimental-result.json").write_text(
                json.dumps(
                    {"head": fixture["head"], "evidence": evidence, "events": events}, indent=2
                ),
                encoding="utf-8",
            )
            browser.close()


if __name__ == "__main__":
    main()
