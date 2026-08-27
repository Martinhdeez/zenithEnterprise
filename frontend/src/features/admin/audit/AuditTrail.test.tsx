/**
 * The screen an administrator scans when they want to know who changed access to what.
 *
 * Two things it must not do: render an internal token where a sentence belongs, and present
 * an automatic event as though a person performed it.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const auditEvents = vi.fn();

vi.mock("../api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../api");
  return { ...actual, auditEvents: (...args: unknown[]) => auditEvents(...args) };
});

const { AuditTrail } = await import("./AuditTrail");

const event = (overrides: Record<string, unknown> = {}) => ({
  id: "e1",
  actor_email: "someone@example.com",
  action: "label.created",
  target_type: "label",
  target_id: "l1",
  target_name: "Finance",
  details: {},
  created_at: "2026-08-25T10:00:00Z",
  ...overrides,
});

beforeEach(() => {
  auditEvents.mockReset();
  auditEvents.mockResolvedValue({ events: [], next_cursor: null });
});

describe("an event a person caused", () => {
  it("names them", async () => {
    auditEvents.mockResolvedValue({ events: [event()], next_cursor: null });

    render(<AuditTrail token="t" />);

    expect(await screen.findByText("someone@example.com")).toBeTruthy();
    expect(await screen.findByText(/created the label/)).toBeTruthy();
  });
});

describe("an event nobody caused", () => {
  it("does not present the classifier as a colleague", async () => {
    // `actor_email` is NOT NULL, so an automatic event has to put something there. Rendering
    // that string beside every human address makes an audit trail where a model looks like
    // someone nobody remembers hiring.
    auditEvents.mockResolvedValue({
      events: [
        event({
          actor_email: "classifier@zenith",
          action: "document.classified",
          target_type: "document",
          target_name: null,
        }),
      ],
      next_cursor: null,
    });

    render(<AuditTrail token="t" />);

    expect(await screen.findByText("Automatic classification")).toBeTruthy();
    expect(screen.queryByText("classifier@zenith")).toBeNull();
  });

  it("reads as a sentence rather than a token", async () => {
    auditEvents.mockResolvedValue({
      events: [event({ actor_email: "classifier@zenith", action: "document.classified" })],
      next_cursor: null,
    });

    render(<AuditTrail token="t" />);

    expect(await screen.findByText(/filed a document automatically/)).toBeTruthy();
    expect(screen.queryByText("document.classified")).toBeNull();
  });
});
