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
  ChevronLeft,
  Building2,
  Check,
  Copy,
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
import { Chat, type Citation } from "@/features/chat";
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
import { ApiError } from "@/shared/api/http";
import { forget, read, write } from "@/shared/lib/storage";
import { copy } from "@/shared/lib/clipboard";

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
  //
  // The track is a control on the ground, so it takes the control step: white `--card` on the
  // grey light ground, the darker well in dark. `bg-background` alone was the ground's own
  // colour in light, and the track vanished.
  return (
    <div role="group" aria-label={t("Language")} className="flex gap-0.5 rounded-full bg-card p-0.5 dark:bg-background">
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
    // Same track as the language control beside it, for the same reason.
    <div role="group" aria-label={t("Theme")} className="flex gap-0.5 rounded-full bg-card p-0.5 dark:bg-background">
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
  // Two seconds of "Copied", then back. A copy button with no acknowledgement leaves the
  // user to test it by pasting somewhere, which defeats the point of the shortcut.
  const [copied, setCopied] = useState(false);
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
  const [collapsedPreference, setCollapsedPreference] = useState(
    () => read("local", SIDEBAR_KEY) === "true",
  );

  /**
   * The rail a *search* asked for, which is not the same thing as the reader's preference and
   * must never be written over it.
   *
   * A search that opens its best result composes the screen around the document: the bar goes
   * to its rail so the passage has the width. That is a property of what is on screen right
   * now, so it lives in ordinary state and dies when the document closes — where `collapsed`
   * below is persisted, and someone who likes the bar open would otherwise find it collapsed
   * for ever after one search.
   */
  const [autoCollapsed, setAutoCollapsed] = useState(false);
  const collapsed = collapsedPreference || autoCollapsed;

  useEffect(() => {
    write("local", SIDEBAR_KEY, String(collapsedPreference));
  }, [collapsedPreference]);

  /**
   * Whether the open document was opened *by* a search rather than clicked.
   *
   * Read by two things that have to agree: the panel opens wider, and `PdfViewer` composes
   * the passage inside it instead of merely scrolling to it.
   */
  const [framed, setFramed] = useState(false);

  // Changing section closes whatever document was open. The preview belongs to the screen
  // that opened it — a PDF left hanging beside the admin panel is a third of the viewport
  // showing something nothing on screen refers to any more.
  // `ask` is the one caller that wants a query carried across: History repeating a past
  // question, and the palette. Everybody else is plain navigation and gets a clean screen.
  const open = useCallback((next: typeof view, ask?: string) => {
    setView(next);
    setCitation(null);
    setPdfExpanded(false);
    setFramed(false);
    setAutoCollapsed(false);
    // The anchored conversation ends with the document it was about.
    //
    // This cleared the citation and left `panel`, `started` and `askQuestion` behind, which
    // put the shell in a state neither half could render: `Search` is hidden while
    // `panel === "conversation"`, and the thread needs the citation that had just been
    // dropped. Both branches false, and the main panel came up blank — reproducibly, by
    // being in a conversation and then pressing Search.
    //
    // Reset rather than preserved, and that is the behaviour rather than an implementation
    // detail: pressing Search is asking for the search screen, not for whatever was on it
    // last time. A thread about a document that is no longer open has nothing to be about.
    setPanel("results");
    setStarted(false);
    // **The last question stopped following the reader around.**
    //
    // `Search` is unmounted by the `view === "search"` guard, so leaving the screen already
    // dropped its query and its results. What survived was `prefill`, up here — and a
    // freshly mounted `Search` runs its prefill effect on mount, so coming back re-ran the
    // search somebody had left behind minutes and three screens ago. Pressing Search is
    // asking for the search screen, not for the last thing that happened on it.
    //
    // Set rather than cleared, because the two callers that *do* want a query carried —
    // History repeating a question, and the palette — go through this same function, and
    // clearing unconditionally would batch their `setPrefill` into oblivion.
    setPrefill(ask ? { text: ask, nonce: Date.now() } : null);
    setAskQuestion(null);
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
      .catch((failure) => {
        if (cancelled) return;
        // **This request is also the session's proof of life, and it is the only one.**
        //
        // A token whose signature still verifies is not the same thing as a session: rebuild
        // the database with `ZENITH_JWT_SECRET` unchanged and every token minted before the
        // rebuild still passes the signature check while naming a `sub` that no longer
        // exists. The API answers that with `404 no such user` — not `401`, because the
        // credential was never in doubt, the person behind it was — and this client used to
        // read it as "the avatar is unavailable" and carry on into a workspace where nothing
        // could ever load.
        //
        // Both statuses mean the same thing here and are treated the same way: whoever this
        // token spoke for cannot be established, so the session is dead and the only honest
        // screen is the login form. Anything else — a timeout, a proxy, the server being
        // down — leaves the session alone and costs an initial in the sidebar.
        if (failure instanceof ApiError && (failure.status === 401 || failure.status === 404)) {
          signOut();
          return;
        }
        setMe(null);
      });
    return () => {
      cancelled = true;
    };
  }, [token, signOut]);

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
  // One renderer for every nav row, because the bar has two groups in it and two copies of
  // this markup would answer a hover differently within a week.
  const navRow = (name: typeof view, Icon: typeof SearchIcon, extra = "", small = false) => (
    <button
      key={name}
      type="button"
      onClick={() => open(name)}
      // `title` and `aria-label` carry the name once the label is gone: an icon alone is a
      // guess for anyone who has not memorised this bar yet, and a screen reader would
      // otherwise hear an unnamed button.
      title={collapsed ? viewLabel(name, t) : undefined}
      aria-label={collapsed ? viewLabel(name, t) : undefined}
      className={`flex items-center rounded-lg text-left transition-colors ${
        collapsed ? "justify-center px-0 py-3" : "gap-3 px-3 py-2"
      } ${
        // A neutral fill and a full-contrast label, with the accent spent on the icon alone.
        // Selection is a state, not an emphasis: the row you are on should be the most
        // *legible*, and the colour is better spent on one small thing than spread across
        // the whole item.
        view === name
          ? "bg-secondary font-medium text-foreground"
          : "text-muted-foreground hover:bg-secondary/40 hover:text-foreground"
      } ${extra}`}
    >
      {/* Larger when collapsed: at this size the icon is the only thing carrying the
          meaning, so it gets the room the label gave up. */}
      <Icon
        className={`shrink-0 ${collapsed ? "size-6" : small ? "size-4" : "size-[18px]"} ${
          view === name ? "text-primary" : ""
        }`}
      />
      {!collapsed && viewLabel(name, t)}
    </button>
  );

  const initial = (me?.name ?? me?.email ?? "").trim().charAt(0).toUpperCase() || "–";

  return (
    // The page ground *is* the sidebar: one surface from the window edge to the workspace,
    // with the navigation drawn straight onto it. The workspace and the preview are the only
    // things inset — rounded, bordered, a gutter clear of the ground — because they are where
    // the work is, and the navigation is how you get there.
    //
    // It used to be three cards floating on a painted backdrop, the sidebar one of them. Three
    // equal surfaces said the navigation mattered as much as the page it serves, and the
    // backdrop was a fourth thing competing for the gutter.
    //
    // The two themes put the light in opposite places. Dark: the ground is `--card` and the
    // workspace sinks to `--background`, the darkest step. Light: the ground is the grey
    // `--background` and the workspace is the white surface, because in a light interface the
    // brightest surface is the one the eye takes for the page. The inversion inside the panel
    // is done with tokens rather than here — `.workspace` in `index.css` — so every screen in
    // it keeps the separation it was tuned with.
    <div className="relative isolate flex h-screen overflow-hidden gap-2 bg-background py-2 pr-2 text-foreground dark:bg-card">
      {/* Layout, not a workspace: Folders and Upload used to live here as their own
          sections, each with its own scroll, competing with navigation for the same
          narrow column. Both are full screens in the main panel now, reached the same way
          Chat or Admin are — this bar's only job left is getting you there and showing
          what's currently ready, which is why Status is the one thing that stayed. */}
      <nav
        className={`flex shrink-0 flex-col transition-[width] duration-200 ${
          collapsed ? "w-16" : "w-72"
        }`}
      >
        {/* Collapsed, this slot holds one control instead of two stacked. The mark and the
            toggle were sharing a 64px column, which made the rail top-heavy and gave the
            eye two targets for what is really one place. Collapsed, the mark steps aside
            and the toggle takes its position — the way Gemini's rail does it — so the
            thing you click to get the sidebar back is exactly where the logo was. */}
        <div
          className={`mx-2 flex shrink-0 items-center gap-2 border-b border-border py-3.5 ${
            collapsed ? "justify-center" : "px-2"
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
              onClick={() => {
                setCollapsedPreference(false);
                // Reaching for the bar ends the search's claim on it. Without this the rail
                // would spring back the moment anything else re-rendered.
                setAutoCollapsed(false);
              }}
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
                onClick={() => setCollapsedPreference(true)}
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
          {/* **One application and five things you add to it.**
              
              The bar presented six peers, and Search is not a peer: it is the screen the
              product exists to be, and the other five are things you do to the corpus it
              searches. Three attempts to say that with colour failed on screen — a wash read
              as a hover, a fill read as loud, a field above the list read as clutter —
              because emphasis inside a list of identical rows is read as a *state* of the
              others rather than as a rank among them.
              
              So the hierarchy is built by demoting the rest rather than promoting one: the
              five sit under a heading at 14px with 16px icons, and Search stays at 16px with
              an 18px icon above them. Two steps of one scale, and no colour anywhere.
              
              The heading is a real `h2` rather than a styled `p`, because screen readers
              navigate by headings — the group has to exist for someone who cannot see the
              gap that makes it. */}
          <div className="px-2 py-3">
            {navRow("search", SearchIcon, "w-full text-base font-medium py-2.5")}
            {!collapsed && (
              <h2 className="mt-5 mb-1 px-3 text-[11px] font-semibold tracking-wider text-muted-foreground/60 uppercase">
                {t("Manage")}
              </h2>
            )}
            <div className="flex flex-col gap-0.5">
              {(
                [
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
                .map(({ name, icon }) => navRow(name, icon, "text-sm py-1.5", true))}
            </div>
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
          // No `panel-accent` here or on the brand row: that ledge is the title bar of an
          // inset panel, and the sidebar is no longer one. On the open ground it painted a
          // darker block at each end of a column that has no edges to hold it, which read as
          // the remains of the card rather than as chrome.
          //
          // `mx-2` on both, so their rules are inset. A divider that runs from the window edge
          // and stops at the gutter reads as a cut in the ground; one that stops short at both
          // ends reads as what it is, a break between groups inside the navigation.
          className={`mx-2 flex shrink-0 flex-col gap-1 border-t border-border py-2 ${
            collapsed ? "items-center" : ""
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
            // `rounded-full`, matching the theme row above it and the language pair beside
            // it rather than approximating them. At `rounded-lg` this was the only control
            // in the footer with corners, and because it hugs its text instead of filling
            // the row, its hover ended in a square edge halfway across — which reads as a
            // clipped block rather than as a pill. The asymmetric padding is part of the
            // same fix: a circular end needs more room after the word than before the
            // avatar, or the text sits against the curve.
            className={`flex items-center rounded-full transition-colors ${
              collapsed ? "justify-center p-1.5" : "min-w-0 gap-2.5 py-1.5 pl-1.5 pr-3"
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
          ask: (question) => open("search", question),
        }}
      />

      <ResizablePanelGroup orientation="horizontal" className="min-w-0 flex-1 gap-3">
        <ResizablePanel
          // Only meaningful while the preview is mounted; with nothing beside it this
          // panel is the entire row regardless of the number.
          //
          // **The pair is normalised, so these two numbers are a ratio and not a pair of
          // widths.** 62 beside the preview's 53 sums to 115, and the group scales both down
          // to fit: the preview lands at 53/115 = 46% of the row, not the 53% it asked for.
          // Measured that way before this line existed. The framed value is therefore the
          // preview's complement rather than a second independent choice.
          defaultSize={framed ? "47%" : "62%"}
          minSize="0%"
          className="workspace flex min-w-0 flex-col rounded-xl border border-border bg-background shadow-sm dark:bg-card"
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
                  {/* `Zenith /` is already rendered above, unconditionally, for every view.
                      This branch used to print it a second time, so the path read
                      `Zenith / Zenith / Search / Chat`. It contributes only the segments
                      this view adds. */}
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
                className="ml-auto flex items-center gap-1.5 rounded-full bg-primary/10 py-1 pl-3 pr-2 text-[0.8125rem] font-medium text-primary transition-colors hover:bg-primary/15"
              >
                {folderSelection?.name ?? "filtered"}
                <X className="size-3.5" />
              </button>
            )}
          </header>

          {/* Chat manages its own scroll region internally — a thread that scrolls with an
              input pinned below it, the way every chat interface this is modelled on does
              — so it gets the bare `overflow-hidden` box that layout requires and none of
              the padding or scrolling every other view here still wants from `main`. */}
          {(
            <main className="flex-1 overflow-auto rounded-b-xl bg-background">
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
            {/* `min-h-full` and a column here offer the panel's full height to whichever
                screen wants it; nothing is centred by this alone, because a screen only
                receives that height by claiming it with `flex-1`. Search is the one that
                does, for its landing state. The rest stay top-aligned, which is what a
                list or a form should be. */}
            <div className={`mx-auto flex min-h-full w-full flex-col p-6 ${measure(view)}`}>
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
              <div className={panel === "conversation" ? "hidden" : "flex flex-1 flex-col"}>
              <Search
                token={token}
                onCitation={(next, question, opened) => {
                  setCitation(next);
                  setAskQuestion(question);
                  // Only the search's own open composes the screen. A citation the reader
                  // clicked leaves the bar, the panel width and the page exactly as they are.
                  setFramed(opened === true);
                  if (opened) setAutoCollapsed(true);
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
            {/* The same `Chat` the product has always had — composer, stop control, thread,
                empty states — given a document to be about. A second chat interface built
                beside it would drift from this one on the first change to either. */}
            {view === "search" && started && citation && askQuestion !== null && (
              <div className={panel === "conversation" ? undefined : "hidden"}>
                {/* Below the bar, not on it. The breadcrumb above says *where you are*; this
                    says *how to leave*, and they are different jobs — one is read, the other
                    is reached for without reading. Sitting them side by side on the same
                    line made two controls that looked like one navigational gesture split in
                    half. Here it sits at the head of the thread it closes, which is where a
                    hand already is.
                    
                    A chevron, not an arrow. `<` is the mark for "back one step" and it does
                    not promise the longer journey an arrow does. */}
                {/* Rendered conditionally although its parent is only *hidden*. The thread
                    stays mounted so looking away does not end it, but this control is chrome
                    rather than state: left in the tree it stayed reachable by keyboard and
                    by a screen reader from a screen it does not belong to, which is what
                    `App.test.tsx` catches by asking for it after the second press. */}
                {panel === "conversation" && (
                  // The mark alone, at a size that carries it. A chevron this large in a
                  // circle of its own is unambiguous without a word beside it, and the words
                  // were doing the arrow's job twice.
                  //
                  // A neutral surface, not the accent. The one accented object on this
                  // screen is the control that opened the conversation, and a second bright
                  // shape would put "go back" and "ask" at the same rank. This is
                  // navigation: it needs to be found instantly and to lose to the thing it
                  // sits above.
                  //
                  // The name lives in `aria-label` and in the tooltip. An icon-only control
                  // is only silent to people who can see it.
                  <button
                    type="button"
                    onClick={() => setPanel("results")}
                    aria-label={t("Back to the results")}
                    title={t("Back to the results")}
                    className="mx-6 mt-4 flex size-9 shrink-0 items-center justify-center rounded-full border border-border bg-secondary text-muted-foreground shadow-sm transition-colors hover:border-primary/40 hover:bg-secondary/70 hover:text-foreground"
                  >
                    <ChevronLeft className="size-5" />
                  </button>
                )}
                <Chat
                  token={token}
                  onCitation={setCitation}
                  searchable={status?.searchable ?? true}
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
                onAsk={(question) => open("search", question)}
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
          // Wider when the search opened it. 35% is the width for a document consulted beside
          // a list being read; the composed open is the document *being* the screen, and 53%
          // is what a page needs before its text is read rather than recognised. The handle
          // still moves it, and a reader who drags it keeps whatever they chose — this is the
          // width it starts at, not one it is held to.
          defaultSize={framed ? "53%" : "35%"}
          minSize="20%"
          maxSize="90%"
          className={
            pdfExpanded
              ? "workspace fixed inset-3 z-50 flex flex-col rounded-xl border border-border bg-background shadow-2xl dark:bg-card"
              : "workspace flex flex-col rounded-xl border border-border bg-background shadow-sm dark:bg-card"
          }
        >
          <header className="panel-accent flex h-12 shrink-0 items-center justify-between rounded-t-xl border-b border-border px-4 text-sm font-medium text-foreground">
            {/* Deliberately not the filename: `PdfViewer` renders its own header with the
                name and page directly below this one, and putting it here too showed it
                twice, stacked. This bar is the panel's chrome — what it is and how to get
                rid of it — and the document identifies itself. */}
            <span className="flex min-w-0 items-center gap-2.5">
            {/* **The one control on this bar that is not chrome, and it is not filed with
                the chrome either.**

                The header is `panel-accent` — the darker ledge, deliberately recessive,
                because a title bar that competes with the document under it is a title bar
                in the way. The demand that this stand out and the surface it sits on are in
                real tension, and there were three ways out: lift this button off the ledge,
                lift the whole ledge, or take the prominence from shape and position instead
                of contrast.

                This is the third. It is a solid accent disc, and it sits at the *left* of
                the bar, beside the panel's name — with the whole width of the header between
                it and the three quiet controls at the other end. Prominence here is not a
                louder version of a ghost icon; it is being a different kind of object, in a
                different place. Filed among the other three it read as a fourth window
                control no matter what colour it was.

                The position also states the grouping the bar always had: this acts on the
                *document*, the three on the right act on the *panel*. Separation across the
                bar says that more plainly than a rule between them did.

                No word. The disc, the accent and the isolation carry it, and the owner's own
                mark is still to come — a label would have to be unlearned when it lands. The
                name travels in `aria-label` and in the tooltip, where a screen reader and a
                hesitating cursor both find it.

                Disabled rather than hidden when there is no question to ask (a document
                opened from the palette or the library): a control that appears and
                disappears is harder to learn than one that is visibly unavailable, and the
                reason travels in its title. Disabled it drops to the panel's own fill —
                same shape, no longer an invitation. */}
            <button
              type="button"
              disabled={askQuestion === null}
              aria-pressed={panel === "conversation"}
              aria-label={t("Ask about this document")}
              title={
                askQuestion === null
                  ? t("Open a document from a search to ask about it")
                  : t("Ask about this document")
              }
              onClick={() => {
                // A toggle, not a one-way door. Pressing it again is the shortest way back
                // to the results, and a control that only ever does half a thing is one the
                // reader has to remember the other half of.
                if (panel === "conversation") {
                  setPanel("results");
                  return;
                }
                setStarted(true);
                setPanel("conversation");
                // `setView`, never `open`. `open` closes the preview — correct for the nav,
                // where a PDF left beside the admin panel refers to nothing on screen, and
                // exactly wrong here: this conversation is *about* the open document, and
                // the whole screen is the answer next to its source. Using `open` closed the
                // document in the same click that started talking about it, which left the
                // panel with nothing to render at all.
                setView("search");
              }}
              className={`flex size-7 shrink-0 items-center justify-center rounded-full transition-all ${
                askQuestion === null
                  ? "cursor-not-allowed bg-secondary text-muted-foreground/70"
                  : panel === "conversation"
                    // **On is louder than off, not quieter.** The first version of this
                    // inverted the fill to a tint on activation, which is the usual way to
                    // draw a pressed toggle and exactly wrong for this one: the moment the
                    // conversation is open is the moment the control matters most, and it
                    // was receding just as the reader needed to find it again. It keeps the
                    // fill and gains a halo — the same object, turned up — and the mark
                    // becomes the way back, because nothing else on screen says the return
                    // is this button pressed a second time.
                    ? "glow-accent bg-primary text-primary-foreground hover:brightness-110"
                    : "bg-primary text-primary-foreground shadow-sm hover:brightness-110"
              }`}
            >
              <MessageSquare className="size-4" />
            </button>
              <span className="truncate">{t("Document preview")}</span>
            </span>
            <span className="flex shrink-0 items-center">
              {/* The passage, not the page. `citation.text` is exactly the span the viewer
                  highlights, so this is the sentence somebody just read and wants to quote —
                  and getting it out of a PDF by hand is a selection across a text layer that
                  fights back. */}
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                disabled={!citation?.text}
                aria-label={copied ? t("Copied") : t("Copy the highlighted passage")}
                title={copied ? t("Copied") : t("Copy the highlighted passage")}
                onClick={() => {
                  if (!citation?.text) return;
                  void copy(citation.text).then((ok) => {
                    // Only on success. Saying "Copied" when the clipboard refused is the
                    // failure this button exists to make impossible.
                    if (!ok) return;
                    setCopied(true);
                    setTimeout(() => setCopied(false), 2000);
                  });
                }}
                className="text-muted-foreground hover:text-foreground"
              >
                {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
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
                  // The composition belonged to the document. Closing it gives the reader
                  // back the bar they had before the search took it.
                  setFramed(false);
                  setAutoCollapsed(false);
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
                <PdfViewer citation={citation} token={token} framed={framed} />
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
