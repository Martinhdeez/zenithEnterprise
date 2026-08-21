/**
 * The shell: sidebar, chat, viewer.
 *
 * Split-screen rather than a modal for the PDF, and that follows from what a citation is
 * for. The user is checking whether the answer's claim matches the page — a modal makes
 * them close the document to re-read the sentence it was supposed to support, which is
 * exactly the comparison the whole feature exists to enable.
 */

import { Suspense, lazy, useCallback, useEffect, useState } from "react";
import {
  Building2,
  Folder as FolderIcon,
  History as HistoryIcon,
  Maximize2,
  MessageSquare,
  Minimize2,
  PanelLeftClose,
  PanelLeftOpen,
  Search as SearchIcon,
  Settings,
  Upload as UploadIcon,
  X,
} from "lucide-react";

import { Admin } from "@/features/admin";
import {
  Login,
  SetPassword,
  Profile,
  profile as fetchMyProfile,
  refreshTokens,
  type UserProfile,
} from "@/features/auth";
import { Chat, type Citation } from "@/features/chat";
import { System } from "@/features/system";
import { Ingesting, inFlight } from "@/features/documents";
import { History } from "@/features/history";
import {
  Folders,
  StatusBadge,
  Upload,
  folders,
  type FolderSelection,
} from "@/features/documents";
import { Search } from "@/features/search";
import { tenantStatus, type TenantStatus } from "@/shared/api/tenant";
import { Section } from "@/shared/components/Section";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { Button } from "@/components/ui/button";

// Lazily loaded, and for a measured reason: `pdf.js` is roughly 1.4 MB of worker plus its
// own runtime, and none of it is needed until someone clicks a citation. F11 made
// time-to-first-answer the metric this client is judged on, and paying for a PDF engine
// before the first question is asked works directly against that.
//
// The one import in this file that deliberately reaches past `@/features/documents` to the
// module itself. Going through the barrel would import the whole feature — `Upload`,
// `Folders`, the API module — to reach one component, which puts all of it in the main
// chunk and leaves nothing behind the `lazy` boundary to split. The rule is "features are
// imported through their public surface"; a code-splitting boundary is the exception, and
// the build output is where it shows: `PdfViewer-*.js` has to stay its own chunk.
const PdfViewer = lazy(() =>
  import("@/features/documents/viewer/PdfViewer").then((module) => ({ default: module.PdfViewer })),
);

// Session storage rather than local storage: it keeps both tokens out of other tabs and out
// of the profile after the browser closes. Not a substitute for the httpOnly cookie an
// eventual hardening pass wants, and recorded as such rather than quietly treated as
// sufficient.
const SIDEBAR_KEY = "zenith.sidebar-collapsed";
const TOKEN_KEY = "zenith.token";
const REFRESH_KEY = "zenith.refresh";

// The access token lives 15 minutes (F2) — deliberately short, so a leaked one is only ever
// briefly useful. The *session* is meant to last as long as the refresh token does (14
// days), and that requires actually using it: refreshing every 10 minutes keeps a 5-minute
// margin against the access token's own expiry without hammering `/auth/refresh` on every
// render.
const REFRESH_INTERVAL_MS = 10 * 60 * 1000;

/** The nav names are lowercase because the sidebar capitalises them in CSS; a `title`
    attribute cannot, so it gets the capital here. */
function capitalise(name: string): string {
  return name.charAt(0).toUpperCase() + name.slice(1);
}

/**
 * The one path that must work before anybody is signed in.
 *
 * Read from `location` rather than routed, because this app has no router: the shell is a
 * `view` union and every screen inside it assumes a token. Adding one for a single public
 * page would be a dependency bought to serve one screen.
 */
function setPasswordToken(): string | null {
  const match = window.location.pathname.match(/^\/set-password\/(.+)$/);
  return match?.[1] ?? null;
}

export function App() {
  const [token, setToken] = useState<string | null>(() => sessionStorage.getItem(TOKEN_KEY));
  const [status, setStatus] = useState<TenantStatus | null>(null);
  const [me, setMe] = useState<UserProfile | null>(null);
  const [citation, setCitation] = useState<Citation | null>(null);
  // A plain union rather than a router. Four screens with no deep links and no back-button
  // expectations do not need one, and a router would be the largest dependency in the
  // bundle for a product whose first screen must render fast on a busy box.
  //
  // "search" and "chat" are deliberately separate views rather than a toggle bolted onto
  // one screen: Search runs the hybrid retrieval alone and shows what it ranked and why —
  // no model reads it, nothing is generated. Chat is retrieval *plus* a model writing prose
  // over what was found. Collapsing them into one screen would make the ask box lie about
  // which of those two things a given answer is.
  // Search is the landing screen, not Chat: it is the one screen that shows what the
  // retrieval mechanism actually did, and that is the more useful first thing to see than
  // an empty ask box — Chat is one click away in the same nav, never removed.
  const [view, setView] = useState<"chat" | "search" | "folders" | "upload" | "history" | "admin" | "system" | "profile">(
    "search",
  );
  // Owned here, not inside `Folders`, so the breadcrumb in the main header can show *and*
  // drive it ("Folders / Tax", each segment clickable) — a second "back" control living
  // only inside that panel would either duplicate the header's or drift from it.
  const [folderSelection, setFolderSelection] = useState<FolderSelection>(null);
  // Chat and Search's own filter, derived rather than tracked separately. Only a specific
  // real folder counts: "All documents" (`filter: null`) and "Unlabelled"
  // (`filter.labelId === null`) both mean "no id to narrow by" for these two — the search
  // API has no way to ask for "only the unlabelled ones" — so both collapse to `null` here
  // exactly the way "nothing selected" already does.
  const folder = folderSelection?.filter?.labelId ?? null;
  const [uploads, setUploads] = useState(0);
  // Set by History when a past question is clicked: a fresh object each time (even for the
  // identical text) so asking the same question twice in a row still re-triggers Chat's
  // effect rather than being a no-op React sees as "the same prop".
  const [prefill, setPrefill] = useState<{ text: string; nonce: number } | null>(null);
  const [pdfExpanded, setPdfExpanded] = useState(false);
  // Remembered across reloads: someone who collapsed the bar to get room back does not
  // want it handed to them again on every refresh. `localStorage` rather than session,
  // because unlike the tokens beside it this is a preference and discloses nothing.
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(SIDEBAR_KEY) === "true",
  );

  useEffect(() => {
    localStorage.setItem(SIDEBAR_KEY, String(collapsed));
  }, [collapsed]);

  // Changing section closes whatever document was open. The preview belongs to the screen
  // that opened it — a PDF left hanging beside the admin panel is a third of the viewport
  // showing something nothing on screen refers to any more.
  const open = useCallback((next: typeof view) => {
    setView(next);
    setCitation(null);
    setPdfExpanded(false);
  }, []);

  const signOut = useCallback(() => {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(REFRESH_KEY);
    setToken(null);
  }, []);

  // A tag chip anywhere — a document row, a search result — narrows the workspace to that
  // label. Resolved by name against the folder tree the server already computes, so a chip
  // for a label this caller cannot reach has nothing to select and does nothing.
  const selectTag = useCallback(
    (name: string) => {
      // Declared above the point where `token` is narrowed by the login guard below, so
      // the check is here rather than in the type.
      if (!token) return;
      void folders(token).then((computed) => {
        const match = computed.folders.find((entry) => entry.name === name);
        if (!match) return;
        setFolderSelection({ name: match.name, filter: { labelId: match.label_id } });
        open("folders");
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [token],
  );

  const refresh = useCallback(async () => {
    if (!token) return;
    try {
      setStatus(await tenantStatus(token));
    } catch {
      // A failed status must not take the application down. Search still works, and
      // `StatusBadge` renders its loading state rather than an error nobody can act on.
      setStatus(null);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    // Only for the avatar and the sidebar; the profile screen fetches its own copy rather
    // than reading a prop, so it is never showing a stale one from before a change.
    void fetchMyProfile(token)
      .then((result) => !cancelled && setMe(result))
      .catch(() => !cancelled && setMe(null));
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(() => {
    // Ingestion is asynchronous, so the counts change without the user doing anything.
    // Thirty seconds is slow enough to be invisible on the network and fast enough that a
    // finished upload appears before anyone reaches for a reload.
    if (!token) return;
    // Adaptive, because the two states want opposite things. Idle, this is a background
    // heartbeat and thirty seconds is already more often than anything changes. Mid-batch
    // it is the only thing telling somebody their thousand files are moving, and half a
    // minute between updates makes a working system look stalled.
    const busy = inFlight(status) > 0;
    const timer = setInterval(() => void refresh(), busy ? 5_000 : 30_000);
    return () => clearInterval(timer);
  }, [token, refresh, status]);

  useEffect(() => {
    if (!token) return;
    const timer = setInterval(() => {
      const held = sessionStorage.getItem(REFRESH_KEY);
      if (!held) return;
      void refreshTokens(held)
        .then((pair) => {
          sessionStorage.setItem(TOKEN_KEY, pair.access_token);
          sessionStorage.setItem(REFRESH_KEY, pair.refresh_token);
          setToken(pair.access_token);
        })
        .catch(() => {
          // The refresh token itself is gone or revoked — nothing left to do but ask the
          // user to sign in again, same as if the access token had simply run out.
          sessionStorage.removeItem(TOKEN_KEY);
          sessionStorage.removeItem(REFRESH_KEY);
          setToken(null);
        });
    }, REFRESH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [token]);

  // Checked before the token, and that order is the whole point: the people who need this
  // page either have no account yet or cannot get into the one they have. Rendering Login
  // first would send them to a form they cannot complete.
  const invitation = setPasswordToken();
  if (invitation) {
    return <SetPassword token={invitation} />;
  }

  if (!token) {
    return (
      <Login
        onAuthenticated={(issued) => {
          sessionStorage.setItem(TOKEN_KEY, issued.access_token);
          sessionStorage.setItem(REFRESH_KEY, issued.refresh_token);
          setToken(issued.access_token);
        }}
      />
    );
  }

  // The signed-in person's initial, not the product's. This was a hardcoded "Z" — the
  // Zenith mark — sitting in an avatar, which reads as a fact about the user and was a
  // fact about the logo. Falls back to the email when no name is set, and to a dash while
  // the profile is still loading rather than to a letter that would be wrong.
  const initial = (me?.name ?? me?.email ?? "").trim().charAt(0).toUpperCase() || "–";

  return (
    // A gutter of plain background, and every region floats on it as its own rounded,
    // bordered card — the sidebar, the workspace and the preview are three surfaces, not
    // one shell with internal dividers, which is the difference between this and the flat
    // edge-to-edge layout it replaced.
    <div className="dark flex h-screen overflow-hidden gap-3 bg-background p-3 text-foreground">
      {/* Layout, not a workspace: Folders and Upload used to live here as their own
          sections, each with its own scroll, competing with navigation for the same
          narrow column. Both are full screens in the main panel now, reached the same way
          Chat or Admin are — this bar's only job left is getting you there and showing
          what's currently ready, which is why Status is the one thing that stayed. */}
      <nav
        className={`flex shrink-0 flex-col rounded-xl border border-border bg-card shadow-sm transition-[width] duration-200 ${
          collapsed ? "w-16" : "w-72"
        }`}
      >
        {/* Collapsed, this slot holds one control instead of two stacked. The mark and the
            toggle were sharing a 64px column, which made the rail top-heavy and gave the
            eye two targets for what is really one place. Collapsed, the mark steps aside
            and the toggle takes its position — the way Gemini's rail does it — so the
            thing you click to get the sidebar back is exactly where the logo was. */}
        <div
          className={`panel-accent flex shrink-0 items-center gap-2 rounded-t-xl border-b border-border py-3.5 ${
            collapsed ? "justify-center px-2" : "px-4"
          }`}
        >
          {collapsed ? (
            // The mark is still the mark until you reach for it. Hovering swaps it for the
            // control, so the rail reads as branding at rest and as a button under the
            // cursor — one slot doing both jobs, which is what keeps a 64px column from
            // needing two rows. `grid` with both children stacked rather than swapping
            // `display`: they occupy the same cell, so nothing shifts on hover.
            <button
              type="button"
              onClick={() => setCollapsed(false)}
              aria-label="Expand sidebar"
              title="Expand sidebar"
              className="group grid size-8 place-items-center rounded-md transition-colors hover:bg-secondary/60"
            >
              <img
                src="/zenith-mark.png"
                alt="Zenith"
                width={22}
                height={22}
                className="col-start-1 row-start-1 size-[22px] object-contain transition-opacity group-hover:opacity-0"
              />
              <PanelLeftOpen
                aria-hidden
                className="col-start-1 row-start-1 size-5 text-muted-foreground opacity-0 transition-opacity group-hover:text-foreground group-hover:opacity-100"
              />
            </button>
          ) : (
            <>
              <img
                src="/zenith-mark.png"
                alt="Zenith"
                width={22}
                height={22}
                className="size-[22px] object-contain"
              />
              <div className="min-w-0 flex-1">
                <p className="text-sm leading-tight font-semibold text-foreground">Zenith</p>
                <p className="text-xs leading-tight text-muted-foreground">Ask your documents</p>
              </div>
              <button
                type="button"
                onClick={() => setCollapsed(true)}
                aria-label="Collapse sidebar"
                title="Collapse sidebar"
                className="shrink-0 rounded-md p-1.5 text-muted-foreground/60 transition-colors hover:bg-secondary/60 hover:text-foreground"
              >
                {/* Matches the collapsed rail's toggle rather than the caption-sized icon
                    it was: the same control on both sides of the same action should not
                    change size depending on which state you are in. */}
                <PanelLeftClose className="size-5" />
              </button>
            </>
          )}
        </div>

        <div className="scrollbar-none flex flex-1 flex-col overflow-y-auto">
          <div className="flex flex-col gap-0.5 px-2 py-3">
            {(
              [
                { name: "search", icon: SearchIcon },
                { name: "chat", icon: MessageSquare },
                { name: "folders", icon: FolderIcon },
                { name: "upload", icon: UploadIcon },
                { name: "history", icon: HistoryIcon },
                { name: "admin", icon: Settings },
                // Above every tenant, so it is above every tenant's nav too: drawn only
                // for the handful of people who hold it. Hiding it is courtesy rather than
                // security — `/system/*` refuses everyone else on its own — but a nav item
                // that always 403s is a worse product than one that is not there.
                { name: "system", icon: Building2 },
              ] as const
            )
              .filter(({ name }) => name !== "system" || me?.is_system_admin)
              .map(({ name, icon: Icon }) => (
              <button
                key={name}
                type="button"
                onClick={() => open(name)}
                // `title` and `aria-label` carry the name once the label is gone: an icon
                // alone is a guess for anyone who has not memorised this bar yet, and a
                // screen reader would otherwise hear an unnamed button.
                title={collapsed ? capitalise(name) : undefined}
                aria-label={collapsed ? capitalise(name) : undefined}
                className={`flex items-center rounded-lg text-left text-[15px] capitalize transition-colors ${
                  collapsed ? "justify-center px-0 py-3" : "gap-3 px-3 py-2"
                } ${
                  view === name
                    ? "bg-primary/10 font-medium text-primary"
                    : "text-muted-foreground hover:bg-secondary/50 hover:text-foreground"
                }`}
              >
                {/* Larger when collapsed: at this size the icon is the only thing carrying
                    the meaning, so it gets the room the label gave up. */}
                <Icon className={collapsed ? "size-6 shrink-0" : "size-[18px] shrink-0"} />
                {!collapsed && name}
              </button>
            ))}
          </div>

          {/* Below the navigation, not above it: this is the answer to "is anything ready
              to search", which is worth glancing at and never the reason you came to this
              bar. Dropped entirely when collapsed — it is prose and a set of numbers, and
              there is no honest way to render either in 64 pixels. */}
          {/* Above the status panel and outside the `!collapsed` guard: ingestion is the
              one thing here worth seeing from a narrow sidebar, because it is the only
              number that changes while you are looking at another screen. */}
          <div className="mt-auto">
            {collapsed ? (
              <Ingesting status={status} collapsed />
            ) : (
              <>
                <Section label="Ingestion">
                  <Ingesting status={status} collapsed={false} />
                </Section>
                <Section label="Status">
                  <StatusBadge status={status} />
                </Section>
              </>
            )}
          </div>
        </div>

        {/* Just the profile now. Signing out moved onto that screen, next to "sign out
            everywhere" — the two are variants of one decision and reading them together is
            what makes the difference between them legible. It also stops a destructive
            action sitting permanently one stray click from the navigation. */}
        <div
          className={`panel-accent flex shrink-0 flex-col gap-1 border-t border-border py-2 ${
            collapsed ? "items-center px-2" : "px-2"
          }`}
        >
          <button
            type="button"
            onClick={() => open("profile")}
            title={collapsed ? "Profile" : undefined}
            aria-label={collapsed ? "Profile" : undefined}
            className={`flex items-center rounded-lg transition-colors ${
              collapsed ? "justify-center p-1.5" : "w-full gap-2.5 px-2 py-1.5"
            } ${
              view === "profile"
                ? "bg-primary/10 text-primary"
                : "text-muted-foreground hover:bg-secondary/50 hover:text-foreground"
            }`}
          >
            <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-secondary text-[11px] font-medium text-muted-foreground">
              {initial}
            </span>
            {!collapsed && <span className="truncate text-sm">Profile</span>}
          </button>
        </div>
      </nav>

      {/* Every size below is a string on purpose: this library reads a bare number as
          pixels, not percent — `defaultSize={65}` is a 65-pixel-wide panel on a 1440px
          screen, which is the bug that made the preview panel render as a sliver. */}
      <ResizablePanelGroup orientation="horizontal" className="min-w-0 flex-1 gap-3">
        <ResizablePanel
          // Only meaningful while the preview is mounted; with nothing beside it this
          // panel is the entire row regardless of the number.
          defaultSize="62%"
          minSize="0%"
          className="flex min-w-0 flex-col rounded-xl border border-border bg-card shadow-sm"
        >
          <header className="panel-accent flex h-12 shrink-0 items-center gap-1.5 rounded-t-xl border-b border-border px-6 text-sm">
            <span className="text-muted-foreground">Zenith</span>
            <span className="text-muted-foreground/50">/</span>
            {view === "folders" ? (
              // A real breadcrumb rather than a static label: `Folders` is one segment when
              // the grid is showing, two once something is selected, and clicking either
              // is a valid destination — the folder browser's whole path lives here now,
              // not behind a "← back" button buried inside the panel below.
              <>
                <button
                  type="button"
                  onClick={() => setFolderSelection(null)}
                  className={
                    folderSelection
                      ? "text-muted-foreground transition-colors hover:text-foreground"
                      : "font-medium text-foreground"
                  }
                >
                  Folders
                </button>
                {folderSelection && (
                  <>
                    <span className="text-muted-foreground/50">/</span>
                    <span className="font-medium text-foreground">{folderSelection.name}</span>
                  </>
                )}
              </>
            ) : (
              <span className="font-medium text-foreground capitalize">{view}</span>
            )}
            {/* Only Chat and Search actually read `folder` — shown only there, so a filter
                picked up in Folders doesn't look like it's still following you into Admin
                or History, where it does nothing. */}
            {folder && (view === "chat" || view === "search") && (
              <button
                type="button"
                onClick={() => setFolderSelection(null)}
                className="ml-auto flex items-center gap-1.5 rounded-full bg-primary/10 py-1 pl-2.5 pr-1.5 text-xs font-medium text-primary transition-colors hover:bg-primary/15"
              >
                {folderSelection?.name ?? "filtered"}
                <X className="size-3" />
              </button>
            )}
          </header>

          {/* Chat manages its own scroll region internally — a thread that scrolls with an
              input pinned below it, the way every chat interface this is modelled on does
              — so it gets the bare `overflow-hidden` box that layout requires and none of
              the padding or scrolling every other view here still wants from `main`. */}
          {view === "chat" ? (
            <div className="flex-1 overflow-hidden rounded-b-xl bg-card">
              <Chat
                token={token}
                onCitation={setCitation}
                searchable={status?.searchable ?? true}
                labels={folder ? [folder] : undefined}
                prefill={prefill}
              />
            </div>
          ) : (
            <main className="flex-1 overflow-auto rounded-b-xl bg-card">
            {/* Every screen is centred and capped here rather than each one setting its own
                width. They used to carry a `max-w-*` and no `mx-auto`, which pinned them to
                the left edge — barely noticeable while the preview panel took a third of the
                row, and obviously wrong the moment that space came back. Capped rather than
                full-bleed because a line of prose spanning a 27" display is unreadable; the
                cap widens one step on very large screens so the extra room is used without
                the measure running away. */}
            <div className="mx-auto w-full max-w-3xl p-6 2xl:max-w-4xl">
            {view === "search" && (
              <Search
                token={token}
                onCitation={setCitation}
                searchable={status?.searchable ?? true}
                labels={folder ? [folder] : undefined}
                onSelectTag={selectTag}
              />
            )}
            {view === "folders" && (
              <Folders
                token={token}
                onCitation={setCitation}
                refreshKey={uploads}
                permissions={me?.permissions}
                selection={folderSelection}
                onSelect={setFolderSelection}
                onSelectTag={selectTag}
              />
            )}
            {view === "upload" && (
              <div>
                <Upload
                  token={token}
                  onUploaded={() => {
                    void refresh();
                    // Bumped so Folders' and Documents' counts follow ingestion. Both are
                    // server-computed, so refreshing them is a fetch rather than a recount.
                    setUploads((count) => count + 1);
                  }}
                />
              </div>
            )}
            {view === "history" && (
              <History
                token={token}
                onAsk={(question) => {
                  setPrefill({ text: question, nonce: Date.now() });
                  open("chat");
                }}
              />
            )}
            {view === "admin" && <Admin token={token} />}
            {view === "system" && <System token={token} />}
            {view === "profile" && (
              <Profile token={token} onSignedOut={signOut} onProfile={setMe} />
            )}
            </div>
            </main>
          )}
        </ResizablePanel>

        {/* The preview and its handle are mounted only while a document is open. An empty
            panel holding "click a citation" was a third of the viewport spent on an
            instruction, permanently, on every screen — including the ones where citations
            are not even reachable. Conditional rather than hidden with a class: unmounting
            is what returns the space to the panel beside it, and `PdfViewer` is lazy, so
            never opening a document means never paying for pdf.js at all. */}
        {citation && (
          <>
        <ResizableHandle withHandle />

        {/* Drag the handle above to resize; the button below goes properly fullscreen for
            the moments dragging is too slow — checking a dense table or a signature page is
            worth the whole viewport for a few seconds. `fixed inset-3` lifts this panel out
            of the flex flow entirely rather than asking `ResizablePanelGroup` to size it to
            100%, which only ever expanded it *within* the row next to the (still visible,
            just squeezed to nothing) chat panel — expanding a share of the layout is not the
            same thing as fullscreen. Position stays fixed on this same element rather than
            porting the content to a portal/overlay, so `PdfViewer` never unmounts and the
            open document doesn't re-fetch or lose its scroll position on the way in or out. */}
        <ResizablePanel
          defaultSize="35%"
          minSize="20%"
          maxSize="90%"
          className={
            pdfExpanded
              ? "fixed inset-3 z-50 flex flex-col rounded-xl border border-border bg-card shadow-2xl"
              : "flex flex-col rounded-xl border border-border bg-card shadow-sm"
          }
        >
          <header className="panel-accent flex h-12 shrink-0 items-center justify-between rounded-t-xl border-b border-border px-4 text-sm font-medium text-foreground">
            {/* Deliberately not the filename: `PdfViewer` renders its own header with the
                name and page directly below this one, and putting it here too showed it
                twice, stacked. This bar is the panel's chrome — what it is and how to get
                rid of it — and the document identifies itself. */}
            Document preview
            <span className="flex shrink-0 items-center">
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={pdfExpanded ? "Exit fullscreen" : "Fullscreen"}
                onClick={() => setPdfExpanded((expanded) => !expanded)}
                className="text-muted-foreground hover:text-foreground"
              >
                {pdfExpanded ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="Close document preview"
                onClick={() => {
                  setCitation(null);
                  setPdfExpanded(false);
                }}
                className="text-muted-foreground hover:text-foreground"
              >
                <X className="size-4" />
              </Button>
            </span>
          </header>
          <div className="flex-1 overflow-auto rounded-b-xl">
            <Suspense
              fallback={<p className="p-6 text-sm text-muted-foreground">Opening the document…</p>}
            >
              <PdfViewer citation={citation} token={token} />
            </Suspense>
          </div>
        </ResizablePanel>
          </>
        )}
      </ResizablePanelGroup>
    </div>
  );
}
