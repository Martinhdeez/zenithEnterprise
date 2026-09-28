"""Authenticated browser checks, using real UI login and the disposable serve.py app.

Run: uv run --with playwright==1.58.0 python ../scripts/evidence_v3_acceptance/browser.py
Uses installed Chrome, isolated browser contexts, and never saves session state.
"""

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

OUT = Path(__file__).resolve().parents[2] / ".scratch/release-validation"
fixture = json.loads((OUT / "fixture.json").read_text(encoding="utf-8"))
events, checks = [], []


def login(page, user):
    page.locator("#email").fill(fixture["users"][user]["email"])
    page.locator("#password").fill(fixture["users"][user]["password"])
    with page.expect_response(lambda r: r.url.endswith("/auth/login")) as response:
        page.locator("#password").press("Enter")
    assert response.value.status == 200
    expect(page.locator("#search-mode")).to_be_visible(timeout=30000)


def search(page, query):
    field = page.locator("textarea[aria-label]").first
    field.fill(query)
    with page.expect_response(lambda r: "/search?" in r.url, timeout=60000) as response:
        field.press("Enter")
    assert response.value.status == 200
    body = response.value.json()
    # Synthetic canaries must never enter an authorized member's response.
    assert "Hiddenlabelcanary" not in json.dumps(body)
    assert "Hiddentenantcanary" not in json.dumps(body)
    return body


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")
        page = context.new_page()
        page.on("pageerror", lambda e: events.append({"pageerror": str(e)}))
        page.on(
            "console",
            lambda m: (
                events.append({"console": m.type, "text": m.text}) if m.type == "error" else None
            ),
        )
        page.on(
            "response",
            lambda r: (
                events.append({"status": r.status, "path": r.url.split("?")[0]})
                if r.status >= 400
                else None
            ),
        )
        try:
            page.goto(fixture["base_url"], wait_until="domcontentloaded", timeout=60000)
            expect(page.locator("#email")).to_be_visible()
            assert context.request.get(fixture["base_url"] + "/search?q=cobalt").status == 401
            checks.append("unauthenticated search refused")
            login(page, "alpha-member")
            checks.append("normal member login via actual UI and auth server")
            alpha_token = page.evaluate("sessionStorage.getItem('zenith.token')")
            alpha_labels = context.request.get(
                fixture["base_url"] + "/labels",
                headers={"Authorization": f"Bearer {alpha_token}"},
            )
            assert alpha_labels.status == 200
            assert fixture["labels"]["alpha"]["private"] not in alpha_labels.text()
            assert "alpha private" not in alpha_labels.text().lower()
            checks.append("normal member cannot enumerate private label metadata")
            assert page.locator("#search-mode option").evaluate_all(
                "xs => xs.map(x => x.value)"
            ) == ["legacy", "hybrid"]
            assert page.locator("#search-mode").input_value() == "legacy"
            body = search(page, "When does the cobalt telescope open?")
            assert not body["degraded"], body
            checks.append(
                {
                    "legacy_real_tei": {
                        "hit_count": len(body["hits"]),
                        "relevance": body["relevance"],
                    }
                }
            )
            # Preserve the legacy lexical-share veto; new semantic mode has no such veto.
            page.locator("#search-mode").focus()
            page.locator("#search-mode").press("ArrowDown")
            expect(page.locator("#search-mode")).to_have_value("hybrid")
            body = search(page, "When does the cobalt telescope open?")
            hit = next(
                h for h in body["hits"] if h["document_id"] == fixture["documents"]["text"]["id"]
            )
            # Click the actual result, even when Search has already opened its top hit.
            page.get_by_role("button").filter(has_text="acceptance-observatory.txt").first.click()
            mark = page.get_by_test_id("citation-highlight")
            expect(mark).to_be_visible(timeout=30000)
            expect(mark).to_contain_text("cobalt telescope opens at midnight")
            assert (
                re.sub(r"\s+", " ", mark.inner_text()).strip()
                == re.sub(r"\s+", " ", hit["text"]).strip()
            )
            page.screenshot(path=str(OUT / "default-text-highlight.png"))
            checks.append("real TEI hybrid search and exact text range visible")
            token = page.evaluate("sessionStorage.getItem('zenith.token')")
            headers = {"Authorization": f"Bearer {token}"}
            for key in ("text", "pdf"):
                doc = fixture["documents"][key]
                downloaded = context.request.get(
                    fixture["base_url"] + f"/documents/{doc['id']}/file", headers=headers
                )
                assert downloaded.status == 200
                assert hashlib.sha256(downloaded.body()).hexdigest() == doc["sha256"]
                if key == "text":
                    assert (
                        mark.inner_text()
                        == downloaded.body().decode()[hit["char_start"] : hit["char_end"]]
                    )
            checks.append("original PDF and text bytes match uploaded SHA256")
            for key in ("label-hidden", "tenant-hidden"):
                doc = fixture["documents"][key]
                for suffix in ("", "/file"):
                    assert (
                        context.request.get(
                            fixture["base_url"] + f"/documents/{doc['id']}{suffix}", headers=headers
                        ).status
                        == 404
                    )
            checks.append("guessed source IDs denied across tenant and label")
            page.get_by_text("alpha public", exact=True).first.click()
            expect(page.locator("#search-mode")).to_have_count(0)
            page.get_by_role("button", name="Search", exact=True).first.click()
            page.locator("#search-mode").select_option("legacy")
            body = search(page, "When does the cobalt telescope open?")
            assert not body["degraded"] and any(
                h["document_id"] == fixture["documents"]["text"]["id"] for h in body["hits"]
            ), body
            page.get_by_role("button").filter(has_text="acceptance-observatory.txt").first.click()
            expect(page.get_by_test_id("citation-highlight")).to_contain_text(
                "cobalt telescope opens at midnight"
            )
            page.screenshot(path=str(OUT / "legacy-scoped-text-highlight.png"))
            checks.append(
                "legacy default with real TEI returns scoped ready source and exact highlight"
            )
            # Reload resets transient scope, retaining only the genuine UI session.
            page.reload(wait_until="domcontentloaded")
            expect(page.locator("#search-mode")).to_be_visible()
            page.locator("#search-mode").select_option("hybrid")
            expect(page.locator("#search-mode")).to_have_value("hybrid")
            body = search(page, "How many credits do saffron submarines pay?")
            hit = next(
                h
                for h in body["hits"]
                if h["document_id"] == fixture["documents"]["pdf"]["id"] and h["page_num"] == 2
            )
            page.get_by_role("button").filter(has_text="Saffron submarines").first.click()
            expect(page.get_by_test_id("pdf-canvas")).to_be_visible(timeout=30000)
            expect(page.get_by_text("Page 2", exact=True)).to_be_visible()
            expect(page.get_by_test_id("citation-highlight").first).to_be_visible()
            assert hit["bboxes"], hit
            page.screenshot(path=str(OUT / "hybrid-pdf-page2-highlight.png"))
            checks.append("keyboard mode switch; reranked PDF original page 2 highlighted")
            page.get_by_role("button", name="Español", exact=True).click()
            expect(page.get_by_text("Modo de búsqueda", exact=True)).to_be_visible()
            page.set_viewport_size({"width": 430, "height": 900})
            expect(page.locator("textarea[aria-label]").first).to_be_visible()
            assert page.locator("textarea[aria-label]").first.bounding_box()["width"] > 100
            expect(page.get_by_text("Página 2", exact=True)).to_be_visible()
            mark = page.get_by_test_id("citation-highlight").first.bounding_box()
            viewport = page.get_by_test_id("pdf-scroller").bounding_box()
            assert (
                mark
                and viewport
                and (
                    min(mark["x"] + mark["width"], viewport["x"] + viewport["width"])
                    > max(mark["x"], viewport["x"])
                )
                and (
                    min(mark["y"] + mark["height"], viewport["y"] + viewport["height"])
                    > max(mark["y"], viewport["y"])
                )
            ), "cited PDF highlight must appear in the narrow viewer"
            page.screenshot(path=str(OUT / "spanish-narrow.png"))
            checks.append("Spanish translation and narrow viewport")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.get_by_role("button", name="English", exact=True).click()
            page.get_by_role("button", name=re.compile("Profile")).first.click()
            page.get_by_role("button", name="Sign out", exact=True).click()
            expect(page.locator("#email")).to_be_visible()
            login(page, "beta-member")
            beta_token = page.evaluate("sessionStorage.getItem('zenith.token')")
            beta_labels = context.request.get(
                fixture["base_url"] + "/labels",
                headers={"Authorization": f"Bearer {beta_token}"},
            )
            assert beta_labels.status == 200
            assert all(
                label_id not in beta_labels.text()
                for label_id in fixture["labels"]["alpha"].values()
            )
            assert "alpha " not in beta_labels.text().lower()
            expect(page.get_by_test_id("citation-highlight")).to_have_count(0)
            assert "cobalt" not in page.locator("body").inner_text().lower()
            assert "saffron" not in page.locator("body").inner_text().lower()
            assert page.evaluate("localStorage.getItem('zenith.recent-searches')") is None
            checks.append("logout and user switch clear source and private recent queries")
            assert not any("pageerror" in e for e in events), events
            print(json.dumps({"checks": checks, "events": events}, indent=2))
        except Exception:
            page.screenshot(path=str(OUT / "failure.png"))
            (OUT / "failure-dom.txt").write_text(
                page.locator("body").inner_text(), encoding="utf-8"
            )
            raise
        finally:
            (OUT / "browser-result.json").write_text(
                json.dumps({"head": fixture["head"], "checks": checks, "events": events}, indent=2),
                encoding="utf-8",
            )
            browser.close()


if __name__ == "__main__":
    main()
