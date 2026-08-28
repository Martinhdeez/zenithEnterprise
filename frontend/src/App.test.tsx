/**
 * The shell: who gets in, what they can see, and what happens when a session ends.
 *
 * `App.tsx` is the largest file in the product and had no test of its own. That is the wrong
 * way round — everything here is the kind of logic whose failure is invisible in review and
 * total in use. A sign-out that leaves the refresh token behind hands the next person at the
 * machine a working session. A nav filter that reads the wrong flag shows every user a panel
 * that governs every tenant. Neither breaks a build.
 *
 * These cover the shell's own decisions and nothing else: the views themselves are mocked,
 * because this file is not where they are tested and rendering them here would make a failure
 * anywhere look like a failure in the shell.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";

const authenticated = vi.fn();
const refreshTokens = vi.fn();
const fetchMyProfile = vi.fn();

vi.mock("@/features/auth", () => ({
  // Renders a button that hands back a token pair, standing in for the whole form.
  Login: ({ onAuthenticated }: { onAuthenticated: (issued: unknown) => void }) => (
    <button type="button" onClick={() => onAuthenticated(authenticated())}>
      sign in
    </button>
  ),
  SetPassword: ({ token }: { token: string }) => <p>set password for {token}</p>,
  // Signing out lives on the profile screen — deliberately, so the destructive control is
  // not one stray click from the navigation. The mock exposes the callback the shell hands
  // it, which is the only part of that arrangement this file is testing.
  Profile: ({ onSignedOut }: { onSignedOut: () => void }) => (
    <button type="button" onClick={onSignedOut}>
      sign out
    </button>
  ),
  profile: (...args: unknown[]) => fetchMyProfile(...args),
  refreshTokens: (...args: unknown[]) => refreshTokens(...args),
}));

vi.mock("@/features/chat", () => ({ Chat: () => <p>chat</p> }));
// Mocked like every other view here: this file tests the shell's decisions, and the
// real one opens a stream whose AbortSignal jsdom refuses — an unhandled rejection that
// would sit in the run masking a real one.
vi.mock("@/features/chat/anchored/AnchoredChat", () => ({
  AnchoredChat: () => <p>anchored chat</p>,
}));
// Emits a citation on demand, so the shell's own reaction to one can be tested. The button
// is inert for every other test in this file, which still only assert that "search" renders.
vi.mock("@/features/search", () => ({
  Search: ({ onCitation }: { onCitation: (c: unknown, q: string) => void }) => (
    <p>
      search
      <button
        type="button"
        onClick={() =>
          onCitation(
            {
              marker: 1,
              chunk_id: "c1",
              document_id: "d1",
              filename: "constitucion.pdf",
              media_type: "application/pdf",
              page_num: 6,
              char_start: 0,
              char_end: 0,
              text: "",
              bboxes: [],
            },
            "plazo máximo",
          )
        }
      >
        emit citation
      </button>
    </p>
  ),
}));
vi.mock("@/features/admin", () => ({ Admin: () => <p>admin</p> }));
vi.mock("@/features/system", () => ({ System: () => <p>system</p> }));
vi.mock("@/features/history", () => ({ History: () => <p>history</p> }));
vi.mock("@/features/labels", () => ({
  TagChips: () => null,
  labels: vi.fn(async () => []),
  LabelPicker: () => null,
}));
vi.mock("@/features/documents", () => ({
  Ingesting: () => null,
  StatusBadge: () => null,
  inFlight: () => 0,
  Folders: () => <p>folders</p>,
  Upload: () => <p>upload</p>,
  PdfViewer: () => null,
  DocumentPanel: () => null,
  folders: vi.fn(async () => ({ folders: [] })),
  tenantStatus: vi.fn(async () => ({
    documents: {},
    chunks: 0,
    hardware: "low-spec",
    components: { embeddings: true, reranker: true, generation: true },
    searchable: true,
  })),
}));

// jsdom ships no `ResizeObserver`, and `react-resizable-panels` constructs one on mount.
// A no-op is the right stub rather than a real implementation: nothing here asserts on
// layout, and the split view's sizing is the browser's job, not this test's.
class NoResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", NoResizeObserver);

const { App } = await import("./App");

const TOKEN_KEY = "zenith.token";
const REFRESH_KEY = "zenith.refresh";

const signedIn = (overrides: Record<string, unknown> = {}) => {
  window.sessionStorage.setItem(TOKEN_KEY, "access-1");
  window.sessionStorage.setItem(REFRESH_KEY, "refresh-1");
  fetchMyProfile.mockResolvedValue({
    user_id: "u1",
    email: "someone@example.com",
    name: "Someone",
    is_system_admin: false,
    ...overrides,
  });
};

beforeEach(() => {
  window.sessionStorage.clear();
  authenticated.mockReturnValue({ access_token: "access-1", refresh_token: "refresh-1" });
  fetchMyProfile.mockResolvedValue({
    user_id: "u1",
    email: "someone@example.com",
    name: "Someone",
    is_system_admin: false,
  });
  refreshTokens.mockReset();
  window.history.replaceState({}, "", "/");
});

afterEach(() => {
  vi.useRealTimers();
});

describe("getting in", () => {
  it("shows the login form when there is no session", () => {
    render(<App />);

    expect(screen.getByRole("button", { name: "sign in" })).toBeTruthy();
  });

  it("keeps both halves of the token pair", async () => {
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "sign in" }));

    await waitFor(() => expect(window.sessionStorage.getItem(TOKEN_KEY)).toBe("access-1"));
    // The refresh token is the half that is easy to forget and the half that keeps somebody
    // signed in past fifteen minutes.
    expect(window.sessionStorage.getItem(REFRESH_KEY)).toBe("refresh-1");
  });

  it("sends an invited user to set a password even while signed in", async () => {
    // The order in `App` is deliberate and worth pinning: the people who need that page
    // either have no account yet or cannot get into the one they have. A session left in
    // this browser must not send them to a workspace instead of the form in their email.
    signedIn();
    // The token is a path segment, not a query parameter — `setPasswordToken` reads
    // `location.pathname`, which is the shape the invitation email actually sends.
    window.history.replaceState({}, "", "/set-password/invite-abc");

    render(<App />);

    expect(await screen.findByText(/set password for invite-abc/)).toBeTruthy();
  });
});

describe("signing out", () => {
  it("removes the refresh token as well as the access token", async () => {
    // Leaving the refresh token behind hands the next person at this machine a session that
    // renews itself. The access token expiring is not protection: the refresh token is
    // exactly the thing that outlives it.
    signedIn();
    render(<App />);
    // Reach the profile screen the way a person does.
    fireEvent.click(await screen.findByRole("button", { name: /Profile/i }));

    fireEvent.click(await screen.findByRole("button", { name: /sign out/i }));

    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(window.sessionStorage.getItem(REFRESH_KEY)).toBeNull();
    expect(screen.getByRole("button", { name: "sign in" })).toBeTruthy();
  });
});

describe("the system panel in the sidebar", () => {
  it("is not offered to an ordinary user", async () => {
    signedIn({ is_system_admin: false });

    render(<App />);
    await screen.findByRole("button", { name: /^search$/i });

    expect(screen.queryByRole("button", { name: /^system$/i })).toBeNull();
  });

  it("is offered to a system administrator", async () => {
    signedIn({ is_system_admin: true });

    render(<App />);

    expect(await screen.findByRole("button", { name: /^system$/i })).toBeTruthy();
  });

  it("stays hidden when the profile could not be loaded", async () => {
    // `fetchMyProfile` failing sets `me` to null, and `me?.is_system_admin` is then
    // undefined. Hiding is the right direction to fail in — the routes refuse everyone else
    // on their own, so this is courtesy, and a nav item that always 403s is a worse product
    // than one that is not there.
    signedIn();
    fetchMyProfile.mockRejectedValue(new Error("offline"));

    render(<App />);
    await screen.findByRole("button", { name: /^search$/i });

    expect(screen.queryByRole("button", { name: /^system$/i })).toBeNull();
  });
});

describe("a session that cannot be renewed", () => {
  it("signs the user out rather than leaving a dead one on screen", async () => {
    // The refresh token is gone or revoked. Doing nothing would leave somebody looking at a
    // workspace where every request 401s, which reads as the product being broken rather
    // than as a session that ended.
    vi.useFakeTimers();
    signedIn();
    refreshTokens.mockRejectedValue(new Error("revoked"));

    render(<App />);
    await act(async () => {
      // Past `REFRESH_INTERVAL_MS`.
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 1_000);
    });

    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBeNull();
    expect(window.sessionStorage.getItem(REFRESH_KEY)).toBeNull();
  });

  it("keeps the new pair when renewal succeeds", async () => {
    vi.useFakeTimers();
    signedIn();
    refreshTokens.mockResolvedValue({ access_token: "access-2", refresh_token: "refresh-2" });

    render(<App />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 1_000);
    });

    expect(window.sessionStorage.getItem(TOKEN_KEY)).toBe("access-2");
    // Rotated, not reused: keeping the old refresh token would defeat rotation entirely.
    expect(window.sessionStorage.getItem(REFRESH_KEY)).toBe("refresh-2");
  });
});

describe("asking about the open document", () => {
  /**
   * The bug this holds: the button called `open("search")`, and `open` clears the citation
   * so a PDF is never left beside a screen that no longer refers to it. Correct for the nav
   * and exactly wrong here — the conversation is *about* the open document, so the click
   * that started it closed the thing it was about, and the panel rendered nothing.
   */
  it("keeps the document open beside the conversation", async () => {
    signedIn();
    await act(async () => {
      render(<App />);
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "emit citation" }));
    });

    // The preview is open, so its header is on screen.
    expect(screen.getByLabelText("Close document preview")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Ask about this document" }));
    });

    // Still open. The conversation replaced the results, not the whole screen.
    expect(screen.getByLabelText("Close document preview")).toBeTruthy();
  });
});
