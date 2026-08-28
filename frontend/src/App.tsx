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
  Monitor,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Search as SearchIcon,
  Settings,
  Sun,
  Upload as UploadIcon,
  X,
} from "lucide-react";

import { setLanguage, useLanguage, useT, type T } from "@/shared/i18n/useT";
import { apply as applyTheme, remember, stored, type Theme } from "@/shared/lib/theme";
import { Admin } from "@/features/admin";
import {
  Login,
  SetPassword,
  Profile,
  profile as fetchMyProfile,
  refreshTokens,
  type UserProfile,
} from "@/features/auth";
import { type Citation } from "@/features/chat";
import { AnchoredChat } from "@/features/chat/anchored/AnchoredChat";
import { System } from "@/features/system";
import { Ingesting, inFlight } from "@/features/documents";
import { History } from "@/features/history";
import { lazyChunk } from "@/shared/lib/lazyChunk";
import {
  Folders,
  StatusBadge,
  Upload,
  folders,
  type FolderSelection,
} from "@/features/documents";
import { Search } from "@/features/search";
import { tenantStatus, type TenantStatus } from "@/shared/api/tenant";
import { CommandPalette } from "@/shared/ui/CommandPalette";
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "@/components/ui/resizable";
import { Button } from "@/components/ui/button";
import { forget, read, write } from "@/shared/lib/storage";

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
const PdfViewer = lazy(
  lazyChunk(() =>
    import("@/features/documents/viewer/PdfViewer").then((module) => ({
      default: module.PdfViewer,
    })),
  ),
);
// Its own chunk, and a much smaller one: this viewer is a `<pre>` and a `<mark>`, while the
// PDF viewer drags pdf.js and its worker behind it. Splitting them means a reader who only
// ever opens Markdown never downloads a PDF engine.
const TextViewer = lazy(
  lazyChunk(() =>
    import("@/features/documents/viewer/TextViewer").then((module) => ({
      default: module.TextViewer,
    })),
  ),
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
 * A view's name in the reader's language.
 *
 * The navigation used to render the view id with `capitalize` in CSS, which works for
 * exactly one language and silently stops working for the next: "upload" is a verb in
 * English and "Subir" in Spanish, and no amount of capitalising gets from one to the other.
 * The id stays the id; this is what a person reads.
 */
function viewLabel(view: string, t: T): string {
  switch (view) {
    case "search": return t("Search");
    case "folders": return t("Folders");
    case "upload": return t("Upload");
    case "history": return t("History");
    case "admin": return t("Admin");
    case "system": return t("System");
    case "profile": return t("Profile");
    default: return capitalise(view);
  }
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

/**
 * How wide a screen is allowed to get. Whole class strings, never built by interpolation:
 * Tailwind scans this file as text, and `max-w-${n}xl` would produce a class that exists in
 * the markup and in no stylesheet.
 */
/**
 * Three settings, not a switch. "System" is what everybody has before they touch anything,
 * and a two-state toggle destroys it on the first click with no way back — somebody who
 * works in a light room by day and a dark one at night would have to flip the application
 * by hand forever after.
 *
 * The choice is applied to `<html>` and the browser is asked again whenever it changes, so
 * a machine that switches at sunset takes the app with it while "System" is selected.
 */
/**
 * Two languages, one control, and no "Auto".
 *
 * The theme has an Auto because a machine has a light-and-dark preference worth following.
 * A browser's `navigator.language` is used once here, to pick the first default, and then
 * the choice is the person's — an interface that silently re-translated itself because
 * somebody opened it on a different machine would be a bug, not a courtesy.
 */
function LanguageControl({ collapsed }: { collapsed: boolean }) {
  const t = useT();
  const language = useLanguage();
  const other = language === "en" ? "es" : "en";
  const NAME = { en: "English", es: "Español" } as const;

  if (collapsed) {
    return (
      <button
        type="button"
        onClick={() => setLanguage(other)}
        title={NAME[other]}
        aria-label={NAME[other]}
        className="flex items-center justify-center rounded-lg p-1.5 text-[11px] font-semibold text-muted-foreground uppercase transition-colors hover:bg-secondary/50 hover:text-foreground"
      >
        {language}
      </button>
    );
  }

  // Codes rather than names, and the same two characters the collapsed rail already shows.
  // "English"/"Español" spelled out needed a row of its own, which is what made the foot of
  // this bar three stacked bands; at two letters the control fits beside the profile and
  // the row disappears. The full name stays in the tooltip and in the accessible name, so
  // nothing is lost to anyone who needs it spelled out.
  return (
    <div role="group" aria-label={t("Language")} className="flex gap-0.5 rounded-full bg-background p-0.5">
      {(["en", "es"] as const).map((value) => (
        <button
          key={value}
          type="button"
          onClick={() => setLanguage(value)}
          aria-pressed={language === value}
          title={NAME[value]}
          aria-label={NAME[value]}
          className={`flex items-center justify-center rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase transition-colors ${
            language === value
              ? "bg-primary/15 text-primary"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          {value}
        </button>
      ))}
    </div>
  );
}

function ThemeControl({ collapsed }: { collapsed: boolean }) {
  const t = useT();
  const [theme, setTheme] = useState<Theme>(stored);

  useEffect(() => {
    applyTheme(theme);
    if (theme !== "system" || typeof matchMedia !== "function") return;
    const media = matchMedia("(prefers-color-scheme: dark)");
    const follow = () => applyTheme("system");
    media.addEventListener("change", follow);
    return () => media.removeEventListener("change", follow);
  }, [theme]);

  const choose = (next: Theme) => {
    setTheme(next);
    remember(next);
  };

  // Records rather than an array indexed by position: `noUncheckedIndexedAccess` is on, and
  // `options[(at + 1) % options.length]` is only provably defined to a human.
  const NEXT: Record<Theme, Theme> = { system: "light", light: "dark", dark: "system" };
  const ICON: Record<Theme, typeof Sun> = { system: Monitor, light: Sun, dark: Moon };
  // "Auto", not "System". `/system` is the system-administration panel and it sits in this
  // same sidebar: two controls a few pixels apart, both reading "System", meaning entirely
  // different things. `App.test.tsx` caught it by asking for a button named System and
  // finding the wrong one, which is exactly what a user would have done.
  const LABEL: Record<Theme, string> = { system: t("Auto"), light: t("Light"), dark: t("Dark") };
  const ORDER: readonly Theme[] = ["system", "light", "dark"];

  // Collapsed, there is no room for three: it cycles instead, and the tooltip names what
  // pressing it will do rather than what is currently on — a control should say what it
  // does, not what it is.
  if (collapsed) {
    const next = NEXT[theme];
    const Icon = ICON[theme];
    return (
      <button
        type="button"
        onClick={() => choose(next)}
        title={t("Switch to {theme}", { theme: LABEL[next].toLowerCase() })}
        aria-label={t("Switch to {theme}", { theme: LABEL[next].toLowerCase() })}
        className="flex items-center justify-center rounded-lg p-1.5 text-muted-foreground transition-colors hover:bg-secondary/50 hover:text-foreground"
      >
        <Icon className="size-4" />
      </button>
    );
  }

  return (
    <div role="group" aria-label={t("Theme")} className="flex gap-0.5 rounded-full bg-background p-0.5">
      {ORDER.map((value) => {
        const Icon = ICON[value];
        return (
        <button
          key={value}
          type="button"
          onClick={() => choose(value)}
          aria-pressed={theme === value}
          title={LABEL[value]}
          className={`flex flex-1 items-center justify-center gap-1.5 rounded-full py-1 text-xs transition-colors ${
            theme === value
              ? "bg-primary/15 text-primary"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          <Icon className="size-3.5" />
          {LABEL[value]}
        </button>
        );
      })}
    </div>
  );
}

function measure(view: string): string {
  switch (view) {
    // A form. Wider only makes the label travel further from its field.
    case "profile":
      return "max-w-2xl";
    // Tables. They were the worst served by a single cap and gain the most from losing it.
    case "admin":
    case "system":
    case "folders":
      return "max-w-6xl 2xl:max-w-7xl";
    // A result, an upload row and a past question are all a name plus a fragment of text.
    //
    // 4xl, not 5xl. 5xl was tried and it used the screen at the cost of looking uncentred:
    // the search bar stretched the full width of the column while the empty state under it
    // stayed a centred block, so the eye got a hard left edge at one width and centred text
    // at another, and read the whole page as shoved left. The gap on each side is what tells
    // you a column is centred, and at 5xl there was not enough of it left to say so.
    default:
      return "max-w-4xl 2xl:max-w-5xl";
  }
}

export function App() {
  const t = useT();
  const [token, setToken] = useState<string | null>(() => read("session", TOKEN_KEY));
  const [status, setStatus] = useState<TenantStatus | null>(null);
  const [me, setMe] = useState<UserProfile | null>(null);
  const [citation, setCitation] = useState<Citation | null>(null);
  // The question that opened the citation, and whether its conversation is showing.
  //
  // Held beside the citation rather than inside it: `Citation` is the shape the server
  // sends for a passage, and the question is something this client knows and the server
  // never said. A document opened from the command palette has no question, which is why
  // this is nullable and why the button that starts a conversation is disabled without it.
  const [askQuestion, setAskQuestion] = useState<string | null>(null);
  // Two facts, not one. `started` is whether a conversation exists for this document;
  // `panel` is which of the two the user is looking at. Collapsing them into one boolean
  // would unmount the conversation every time somebody stepped back to the results through
  // the breadcrumb — and a remounted conversation re-asks, which spends a generation the
  // user did not request and replaces the thread they were reading.
  const [started, setStarted] = useState(false);
  const [panel, setPanel] = useState<"results" | "conversation">("results");
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
  const [view, setView] = useState<"search" | "folders" | "upload" | "history" | "admin" | "system" | "profile">(
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
  //
  // **Guarded on both sides, and here that is not a nicety.** `localStorage` is absent or
  // throws in Safari's private browsing and under enterprise policies that block site data,
  // and this call sits in the render path of the whole application: unguarded, a blocked
  // preference store took down the entire product rather than one sidebar setting. `Search`
  // learned the same lesson where it cost a search result; this is the version that costs
  // everything.
  const [collapsed, setCollapsed] = useState(() => read("local", SIDEBAR_KEY) === "true");

  useEffect(() => {
    write("local", SIDEBAR_KEY, String(collapsed));
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
    forget("session", TOKEN_KEY);
    forget("session", REFRESH_KEY);
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
      const held = read("session", REFRESH_KEY);
      if (!held) return;
      void refreshTokens(held)
        .then((pair) => {
          write("session", TOKEN_KEY, pair.access_token);
          write("session", REFRESH_KEY, pair.refresh_token);
          setToken(pair.access_token);
        })
        .catch(() => {
          // The refresh token itself is gone or revoked — nothing left to do but ask the
          // user to sign in again, same as if the access token had simply run out.
          forget("session", TOKEN_KEY);
          forget("session", REFRESH_KEY);
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
          write("session", TOKEN_KEY, issued.access_token);
          write("session", REFRESH_KEY, issued.refresh_token);
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
    <div className="flex h-screen overflow-hidden gap-3 bg-background p-3 text-foreground">
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
              aria-label={t("Expand sidebar")}
              title={t("Expand sidebar")}
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
                <p className="text-xs leading-tight text-muted-foreground">{t("Ask your documents")}</p>
              </div>
              <button
                type="button"
                onClick={() => setCollapsed(true)}
                aria-label={t("Collapse sidebar")}
                title={t("Collapse sidebar")}
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
                title={collapsed ? viewLabel(name, t) : undefined}
                aria-label={collapsed ? viewLabel(name, t) : undefined}
                className={`flex items-center rounded-lg text-left text-[15px] transition-colors ${
                  collapsed ? "justify-center px-0 py-3" : "gap-3 px-3 py-2"
                } ${
                  // A neutral fill and a full-contrast label, with the accent spent on the
                  // icon alone.
                  //
                  // It was `bg-primary/10 text-primary` — a translucent blue block with
                  // blue text, which is shadcn's default and reads as a highlighter mark
                  // rather than as a selected row. Tinting both the surface and the text
                  // the same hue also leaves the label washed out at the exact moment it
                  // matters most.
                  //
                  // Selection is a state, not an emphasis: the row you are on should be the
                  // most *legible*, and the colour is better spent on one small thing than
                  // spread across the whole item.
                  view === name
                    ? "bg-secondary font-medium text-foreground"
                    : "text-muted-foreground hover:bg-secondary/40 hover:text-foreground"
                }`}
              >
                {/* Larger when collapsed: at this size the icon is the only thing carrying
                    the meaning, so it gets the room the label gave up. */}
                <Icon
                  className={`shrink-0 ${collapsed ? "size-6" : "size-[18px]"} ${
                    view === name ? "text-primary" : ""
                  }`}
                />
                {!collapsed && viewLabel(name, t)}
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
                {/* The ingestion detail only while there is ingestion. The bar inside
                    `StatusBadge` is drawn at every count and already says "nothing is
                    moving" by being wholly one colour, so the prose version below it is
                    the progress figure and the warning about slower searches — both of
                    which have nothing to report when the queue is empty. */}
                <div className="px-4 py-3">
                  <StatusBadge status={status}>
                    {inFlight(status) > 0 ? <Ingesting status={status} collapsed={false} /> : null}
                  </StatusBadge>
                </div>
              </>
            )}
          </div>
        </div>

        {/* Just the profile now. Signing out moved onto that screen, next to "sign out
            everywhere" — the two are variants of one decision and reading them together is
            what makes the difference between them legible. It also stops a destructive
            action sitting permanently one stray click from the navigation. */}
        <div
          // `rounded-b-xl` mirrors the `rounded-t-xl` on the brand header at the other end
          // of this column. Both are `panel-accent`, which paints a gradient rather than
          // inheriting the sidebar's fill, so a square corner here does not just fail to
          // curve — it paints over the curve, and the sidebar reads as having one rounded
          // corner and one blunt one.
          className={`panel-accent flex shrink-0 flex-col gap-1 rounded-b-xl border-t border-border py-2 ${
            collapsed ? "items-center px-2" : "px-2"
          }`}
        >
          {collapsed && <LanguageControl collapsed />}
          <ThemeControl collapsed={collapsed} />
          {/* Two rows where there were three. The theme keeps a row to itself because it
              keeps its three words: "Auto" and "Claro" say what they do and an icon does
              not, and a three-way choice is the one control here that is not obvious from
              its shape. The language pair is two characters wide, so it rides on the
              profile row instead of claiming a band of its own. */}
          <div className={collapsed ? "contents" : "flex items-center gap-2"}>
          <button
            type="button"
            onClick={() => open("profile")}
            title={collapsed ? t("Profile") : undefined}
            aria-label={collapsed ? t("Profile") : undefined}
            className={`flex items-center rounded-lg transition-colors ${
              collapsed ? "justify-center p-1.5" : "min-w-0 gap-2.5 px-2 py-1.5"
            } ${
              view === "profile"
                ? "bg-primary/10 text-primary"
                : "text-muted-foreground hover:bg-secondary/50 hover:text-foreground"
            }`}
          >
            <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-secondary text-[11px] font-medium text-muted-foreground">
              {initial}
            </span>
            {!collapsed && <span className="truncate text-sm">{t("Profile")}</span>}
          </button>
          {!collapsed && (
            <div className="ml-auto">
              <LanguageControl collapsed={false} />
            </div>
          )}
          </div>
        </div>
      </nav>

      {/* Every size below is a string on purpose: this library reads a bare number as
          pixels, not percent — `defaultSize={65}` is a 65-pixel-wide panel on a 1440px
          screen, which is the bug that made the preview panel render as a sliver. */}
      {/* Global, and mounted once: the shortcut is registered on the window, so it works
          from every screen without each of them knowing about it. */}
      <CommandPalette
        token={token}
        actions={{
          go: (next) => open(next as typeof view),
          openDocument: (document) => {
            // The viewer wants a citation; a document opened from the palette has no
            // passage behind it, so page one with no highlights is the honest shape —
            // rather than inventing bounding boxes that point at nothing.
            setCitation({
              marker: 0,
              chunk_id: "",
              document_id: document.id,
              filename: document.filename,
              media_type: document.media_type,
              // Opened from the library rather than from an answer, so there is no cited
              // passage: the first page and no highlight for a PDF, the top of the file
              // and an empty range for a text document. Inventing either would point the
              // reader at something the corpus never said.
              page_num: document.media_type.startsWith("text/") ? null : 1,
              char_start: 0,
              char_end: 0,
              text: "",
              bboxes: [],
            });
          },
          ask: (question) => {
            setPrefill({ text: question, nonce: Date.now() });
            open("search");
          },
        }}
      />

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
                  {t("Folders")}
                </button>
                {folderSelection && (
                  <>
                    <span className="text-muted-foreground/50">/</span>
                    <span className="font-medium text-foreground">
                      {/* Only the one name this application owns. Every other value here is
                          a label the customer created, and translating those would rename
                          their own filing in front of them. */}
                      {folderSelection.name === "All documents"
                        ? t("All documents")
                        : folderSelection.name}
                    </span>
                  </>
                )}
              </>
            ) : (
              // The page's actual title, so it is the page's `h1`. It was a `span`, and the
              // whole application had zero `h1` elements — no outline for a screen reader,
              // and nowhere for a typographic hierarchy to attach. One cause, one fix.
              // Three segments while a conversation is open, one otherwise. The same
              // grammar the folder path above uses — clickable segment, muted separator,
              // current segment in `font-medium` — rather than a second breadcrumb with its
              // own rules sitting on the same bar.
              started && panel === "conversation" && view === "search" ? (
                <>
                  {/* The one name this application owns, and the one segment that is never
                      translated. */}
                  <span className="text-muted-foreground">Zenith</span>
                  <span className="text-muted-foreground/50">/</span>
                  <button
                    type="button"
                    onClick={() => setPanel("results")}
                    className="text-muted-foreground transition-colors hover:text-foreground"
                  >
                    {t("Search")}
                  </button>
                  <span className="text-muted-foreground/50">/</span>
                  <h1 className="text-[15px] font-medium text-foreground">{t("Chat")}</h1>
                </>
              ) : (
                <h1 className="text-[15px] font-medium text-foreground">{viewLabel(view, t)}</h1>
              )
            )}
            {/* Only Chat and Search actually read `folder` — shown only there, so a filter
                picked up in Folders doesn't look like it's still following you into Admin
                or History, where it does nothing. */}
            {folder && view === "search" && (
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
          {(
            <main className="flex-1 overflow-auto rounded-b-xl bg-card">
            {/* Every screen is centred and capped here rather than each one setting its own
                width. They used to carry a `max-w-*` and no `mx-auto`, which pinned them to
                the left edge — barely noticeable while the preview panel took a third of the
                row, and obviously wrong the moment that space came back.

                The cap is per view, which it was not: one value of `max-w-3xl` covered a
                form, a list of results and a table alike, and 768px of a 1114px panel leaves
                31% of the working area empty on an ordinary laptop. Capped rather than
                full-bleed still, because the reason for a cap is real — a line of prose
                across a 27" display is unreadable — but that reason is about prose, and only
                two of these screens are prose. A table is the opposite: it wants every pixel
                it can have, and cramming one into a reading measure is what produces the
                columns nobody can read. */}
            <div className={`mx-auto w-full p-6 ${measure(view)}`}>
            {/* Kept mounted, not unmounted, while its conversation is showing.
                `Search` owns its results, its query and its resolved filenames in its own
                state, so `{view === "search" && <Search/>}` destroyed all of it the moment
                the panel switched — and the breadcrumb above promises the opposite: that
                going back lands on the results you had, ready to pick a different passage.
                Hiding costs a subtree that stays rendered; for fifty passages that is not a
                cost worth designing around. Lifting the state into this component instead
                would move five pieces of state and their effects into the largest file in
                the tree. */}
            {view === "search" && (
              <div className={panel === "conversation" ? "hidden" : undefined}>
              <Search
                token={token}
                onCitation={(next, question) => {
                  setCitation(next);
                  setAskQuestion(question);
                  // A new document ends the previous conversation rather than silently
                  // re-pointing it: the thread that was on screen was about a different
                  // file, and carrying it over would attribute its answers to this one.
                  setStarted(false);
                  setPanel("results");
                }}
                // Which result the viewer is showing, so the list can mark it. Read from
                // the citation rather than tracked inside `Search`: closing the viewer sets
                // this to null, and a copy kept in the list would stay lit over a panel
                // that is no longer open.
                openChunkId={citation?.chunk_id ?? null}
                searchable={status?.searchable ?? true}
                labels={folder ? [folder] : undefined}
                onSelectTag={selectTag}
                // The empty result is where a forgotten folder filter finally becomes
                // visible, so it gets both the name and the way out.
                filterName={folderSelection?.name ?? null}
                onClearFilter={() => setFolderSelection(null)}
                prefill={prefill}
              />
              </div>
            )}
            {/* Mounted from the moment the conversation is started and hidden — never
                unmounted — for the same reason `Search` is: it holds the thread, and the
                breadcrumb is a way to look away from it, not a way to end it. */}
            {view === "search" && started && citation && askQuestion !== null && (
              <div className={panel === "conversation" ? undefined : "hidden"}>
                <AnchoredChat
                  token={token}
                  onCitation={(next) => setCitation(next)}
                  anchor={{
                    documentId: citation.document_id,
                    filename: citation.filename,
                    question: askQuestion,
                  }}
                />
              </div>
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
                  open("search");
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
            {t("Document preview")}
            <span className="flex shrink-0 items-center">
              {/* First, because it is the only control here that belongs to the *document*
                  rather than to the panel — the two beside it resize and close the frame.
                  Disabled rather than hidden when there is no question to ask (a document
                  opened from the palette or the library): a control that appears and
                  disappears is harder to learn than one that is visibly unavailable, and the
                  reason travels in its title. */}
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                disabled={askQuestion === null || panel === "conversation"}
                aria-label={t("Ask about this document")}
                title={
                  askQuestion === null
                    ? t("Open a document from a search to ask about it")
                    : t("Ask about this document")
                }
                onClick={() => {
                  setStarted(true);
                  setPanel("conversation");
                  // `setView`, never `open`. `open` closes the preview — correct for the
                  // nav, where a PDF left beside the admin panel refers to nothing on
                  // screen, and exactly wrong here: this conversation is *about* the open
                  // document, and the whole screen is the answer next to its source. Using
                  // `open` closed the document in the same click that started talking about
                  // it, which left the panel with nothing to render at all.
                  setView("search");
                }}
                className="text-muted-foreground hover:text-foreground"
              >
                <MessageSquare className="size-4" />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={pdfExpanded ? t("Exit fullscreen") : t("Fullscreen")}
                onClick={() => setPdfExpanded((expanded) => !expanded)}
                className="text-muted-foreground hover:text-foreground"
              >
                {pdfExpanded ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label={t("Close document preview")}
                onClick={() => {
                  setCitation(null);
                  setPdfExpanded(false);
                  // The conversation was about the document being closed. Leaving it on
                  // screen would leave answers with no source beside them to check.
                  setStarted(false);
                  setPanel("results");
                  setAskQuestion(null);
                }}
                className="text-muted-foreground hover:text-foreground"
              >
                <X className="size-4" />
              </Button>
            </span>
          </header>
          <div className="flex-1 overflow-auto rounded-b-xl">
            <Suspense
              fallback={<p className="p-6 text-sm text-muted-foreground">{t("Opening the document…")}</p>}
            >
              {/* Chosen from the document's stored media type, never from its filename.
                  A `.txt` opened in the PDF frame is the mixed-list problem this feature
                  was careful to avoid: a broken PDF sitting beside real ones. */}
              {citation && citation.media_type.startsWith("text/") ? (
                <TextViewer citation={citation} token={token} />
              ) : (
                <PdfViewer citation={citation} token={token} />
              )}
            </Suspense>
          </div>
        </ResizablePanel>
          </>
        )}
      </ResizablePanelGroup>
    </div>
  );
}
