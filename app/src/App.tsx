import { useEffect, useMemo, useRef, useState } from "react"
import { Card, CardContent } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import { cn } from "@/lib/utils"
import { Settings2, Bookmark } from "lucide-react"
import { Progress } from "@/components/ui/progress"
import { Toggle } from "@/components/ui/toggle"

// The dashboard server now issues a per-launch capability token and refuses
// unauthenticated /api calls, so a stray local process or a web page cannot
// drive restore. The page receives it in its own URL and echoes it back.
// The token still arrives in the URL, because that is what lets an already
// open tab keep working after the server restarts. The server hands back an
// HttpOnly cookie on first load, so once that exists the URL copy is no longer
// needed and is removed from the address bar: it should not sit in browser
// history or in a screenshot.
const TOKEN_Q = (() => {
  const t = new URLSearchParams(window.location.search).get("t");
  if (t) {
    // Wait until the cookie is definitely set, i.e. after this document was
    // served, then drop the token from the visible URL without navigating.
    setTimeout(() => {
      try {
        const u = new URL(window.location.href);
        u.searchParams.delete("t");
        window.history.replaceState({}, "", u.pathname + u.search + u.hash);
      } catch { /* leaving it visible is not worth an exception */ }
    }, 0);
  }
  return t ? "?t=" + encodeURIComponent(t) : "";
})();

type Session = {
  id: string; short: string; folder: string; cwd: string; prompt: string
  state: string; badge: string; ago: string; window: string
  windowLabel: string; running: boolean; host: string; isCurrent: boolean
  sessionName: string; lifetime: string; lastSeen: number; how: string
  gone: boolean; predecessor: string; successor: string; restorable: boolean
  host_source: "observed" | "declared" | "default" | "unknown"
  bookmarked: boolean
}
type Folder = { name: string; cwd: string; count: number }
type Skill = { name: string; description: string }
type State = {
  sessions: Session[]; folders: Folder[]; skills: Skill[]; windows: number
  statusOrder: string[]
  default_host: string; available_hosts: string[]; bookmarks: string[]
  sort_mode: "label" | "date"
  host_evidence: Record<string, string>; missing_hosts: string[]
}

// Two axes, two rows: what happened, and when. Kept separate so neither list
// grows unreadable, and "All" is the default because hiding sessions was the
// bug we just fixed. Narrowing is opt-in.
// Cumulative "how far back", except Today and Yesterday which are calendar
// days, because that is what those words mean. Each chip shows its own count
// so the non-nesting is visible rather than surprising.
type Range = { label: string; hint: string; from: () => number; to?: () => number }
const startOfToday = () => new Date(new Date().setHours(0, 0, 0, 0)).getTime() / 1000
const DAY = 86400
const EVIDENCE: Record<string, string> = {
  observed:  "seen hosting a shell right now",
  plugin:    "the app cannot, but a plugin installed in it can",
  pty:       "its binaries link the pty syscalls",
  cli:       "a command-line host, no app needed",
  installed: "installed, capability not confirmed",
}

const RANGES: Range[] = [
  { label: "Today",     hint: "active since midnight",        from: startOfToday },
  { label: "Yesterday", hint: "active yesterday only",
    from: () => startOfToday() - DAY, to: startOfToday },
  { label: "7 days",    hint: "last 7 days",   from: () => Date.now() / 1000 - 7 * DAY },
  { label: "15 days",   hint: "last 15 days",  from: () => Date.now() / 1000 - 15 * DAY },
  { label: "30 days",   hint: "last 30 days",  from: () => Date.now() / 1000 - 30 * DAY },
  { label: "All time",  hint: "everything",    from: () => 0 },
]

type Theme = "light" | "dark"
function applyTheme(t: Theme) {
  const r = document.documentElement
  r.classList.remove("light", "dark")
  r.classList.add(t)                       // always explicit, never "system"
  try { localStorage.setItem("revive-theme", t) } catch { /* private window */ }
}

/* Filled chips with black text, per the sketch. */
const BADGE: Record<string, string> = {
  CRASHED:    "bg-[#ef4444] text-black hover:bg-[#ef4444]",
  BACKFILL:   "bg-[#ef4444] text-black hover:bg-[#ef4444]",   // inferred crash
  TERMINATED: "bg-[#f59e0b] text-black hover:bg-[#f59e0b]",
  EXITED:     "bg-[#22c55e] text-black hover:bg-[#22c55e]",   // ended cleanly
  RUNNING:    "bg-[#a1a1aa] text-black hover:bg-[#a1a1aa]",
}

function shortPath(p: string) {
  const parts = (p || "").split("/").filter(Boolean)
  return parts.length <= 3 ? p : ".../" + parts.slice(-3).join("/")
}

export default function App() {
  const [data, setData] = useState<State>({
    sessions: [], folders: [], skills: [], windows: 0, statusOrder: [],
    default_host: "", available_hosts: [], host_evidence: {},
    missing_hosts: [], bookmarks: [], sort_mode: "label" })
  const [range, setRange] = useState<string>("All time")
  const [showSettings, setShowSettings] = useState(false)
  const [savingHost, setSavingHost] = useState(false)
  const [theme, setTheme] = useState<Theme>(() => {
    try { return (localStorage.getItem("revive-theme") as Theme) || "light" }
    catch { return "light" }
  })
  useEffect(() => applyTheme(theme), [theme])


  // Re-read when you come back to the tab. Exiting a revived session changes
  // its state, and the dashboard was showing whatever was true when it loaded.
  // Focus is the right trigger: polling would rescan the transcripts on a timer
  // for nothing most of the time.
  useEffect(() => {
    const onFocus = () => { if (!document.hidden) reload() }
    window.addEventListener("focus", onFocus)
    document.addEventListener("visibilitychange", onFocus)
    return () => {
      window.removeEventListener("focus", onFocus)
      document.removeEventListener("visibilitychange", onFocus)
    }
  }, [])
  const [status_, setStatus_] = useState<string | null>(null)   // badge filter
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [folder, setFolder] = useState<string | null>(null)
  const [query, setQuery] = useState("")
  const [sq, setSq] = useState("")                 // sessions search box
  const [convoIds, setConvoIds] = useState<Set<string> | null>(null)
  const [searching, setSearching] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [loaded, setLoaded] = useState(false)

  const toggleBookmark = async (id: string, on: boolean) => {
    setData(d => ({
      ...d,
      bookmarks: on ? [...d.bookmarks, id] : d.bookmarks.filter(x => x !== id),
      sessions: d.sessions.map(s => s.id === id ? { ...s, bookmarked: on } : s),
    }))
    try {
      await fetch("/api/bookmark" + TOKEN_Q, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, on }),
      })
    } catch { /* the next reload re-reads the truth from the registry */ }
  }

  // ok = the server answered, retrying = a request is in flight or we are
  // re-attempting after a failure, down = it is not answering. The dot in the
  // header shows this, so the page can never look healthy while it is serving
  // stale data.
  const [conn, setConn] = useState<"ok" | "retrying" | "down">("ok")
  const offline = conn === "down"

  // A failed refresh used to do NOTHING visible: the fetch rejected, the catch
  // swallowed it, and the page kept showing stale data. So a session you had
  // exited still read "Crashed" and Refresh looked broken, when in fact the
  // server was gone. Say so.
  const reload = async () => {
    setRefreshing(true)
    setConn(c => (c === "down" ? "retrying" : c))
    try {
      const r = await fetch("/api/state" + TOKEN_Q)
      if (!r.ok) throw new Error("HTTP " + r.status)
      setData(await r.json())
      setConn("ok")
    } catch {
      setConn("down")
    } finally { setRefreshing(false) }
  }
  // Near-real-time updates. /api/state costs ~0.7s, so polling it would be
  // wasteful and slow; /api/pulse stats two directories in ~1ms and returns a
  // hash. Poll the cheap one every 2s and fetch the full state only when the
  // hash moves. A /rename or an exit then appears on its own, instead of when
  // you happen to click back onto the page.
  // The two scroll panes must end just above the fixed action bar. A constant
  // like calc(100vh - 11rem) was wrong: the header block (title, note, tabs,
  // search, two filter rows) changes height with the window and with wrapping,
  // so the panes ran underneath the bar. Measure instead.
  // Independent panes only make sense side by side. Stacked, a tall scroller
  // for the folder rail would eat the screen and push the cards out of view,
  // so below md the page scrolls normally and the rail is simply capped.
  const [wide, setWide] = useState(
    typeof window !== "undefined" ? window.matchMedia("(min-width: 768px)").matches : true)
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 768px)")
    const on = () => setWide(mq.matches)
    mq.addEventListener("change", on)
    return () => mq.removeEventListener("change", on)
  }, [])

  const panesRef = useRef<HTMLDivElement | null>(null)
  const barRef = useRef<HTMLDivElement | null>(null)
  const [paneH, setPaneH] = useState<number | null>(null)
  const [barH, setBarH] = useState(56)
  useEffect(() => {
    const measure = () => {
      const top = panesRef.current?.getBoundingClientRect().top ?? 0
      const bar = barRef.current?.offsetHeight ?? 56
      setBarH(bar)
      setPaneH(Math.max(240, window.innerHeight - top - bar - 16))
    }
    measure()
    window.addEventListener("resize", measure)
    const id = setInterval(measure, 800)      // header height changes with filters
    return () => { window.removeEventListener("resize", measure); clearInterval(id) }
  }, [])

  const pulseRef = useRef<string>("")
  useEffect(() => {
    let stop = false
    const tick = async () => {
      if (stop) return
      try {
        const r = await fetch("/api/pulse" + TOKEN_Q)
        if (!r.ok) throw new Error("HTTP " + r.status)
        const { pulse } = await r.json()
        setConn(c => (c === "ok" ? c : "ok"))
        if (pulseRef.current && pulse !== pulseRef.current) reload()
        pulseRef.current = pulse
      } catch {
        setConn("down")
      }
    }
    tick()
    const t = setInterval(tick, 2000)
    return () => { stop = true; clearInterval(t) }
  }, [])

  // Keep trying while it is down, so the dot returns to green on its own once
  // /revive brings the server back. Without this the page stays red until you
  // remember to press Refresh.
  useEffect(() => {
    if (conn !== "down") return
    const t = setInterval(() => { reload() }, 5000)
    return () => clearInterval(t)
  }, [conn])

  // cwd / name / prompt match instantly on data already in the browser.
  // The CONVERSATION lives only in the transcripts, so that half is a server
  // query, run on Enter rather than per keystroke: it greps 1.5 GB.
  const searchConversations = async () => {
    const term = sq.trim()
    if (term.length < 2) { setConvoIds(null); return }
    setSearching(true)
    try {
      const r = await fetch("/api/search" + (TOKEN_Q ? TOKEN_Q + "&" : "?") +
                            "q=" + encodeURIComponent(term))
      const d = await r.json()
      setConvoIds(new Set<string>(d.ids || []))
    } catch { setConvoIds(new Set<string>()); setConn("down") }
    finally { setSearching(false) }
  }
  const [status, setStatus] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    fetch("/api/state" + TOKEN_Q).then(r => r.json()).then((d: State) => {
      setData(d)
      setLoaded(true)
      // Deliberately select NOTHING. Pre-ticking made sense at 20 sessions;
      // with the day limit lifted it would arm 150+, and restoring that many
      // at once is exactly the memory spike that caused the crash this tool
      // exists to recover from. "Select all" is one click away.
      setPicked(new Set())
    })
  }, [])

  const inRange = useMemo(() => {
    const r = RANGES.find(x => x.label === range) || RANGES[RANGES.length - 1]
    const from = r.from(), to = r.to ? r.to() : Infinity
    return data.sessions.filter(s => {
      const t = s.lastSeen || 0
      return t >= from && t < to
    })
  }, [data.sessions, range])

  // Counts follow the active range, so the chips never promise rows the
  // current view cannot show.
  const statusCounts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const s of inRange) c[s.badge] = (c[s.badge] || 0) + 1
    return c
  }, [inRange])

  // "Bookmarked" sits in the same pill row as the states but is not a state:
  // a session is bookmarked AND crashed. Treated as its own predicate.
  const inScope = useMemo(
    () => inRange.filter(s => status_ === null
                              || (status_ === "Bookmarked" ? s.bookmarked
                                                           : s.badge === status_)),
    [inRange, status_])

  const matchesText = (s: Session) => {
    const t = sq.trim().toLowerCase()
    if (!t) return true
    if (s.cwd.toLowerCase().includes(t)) return true
    if ((s.sessionName || "").toLowerCase().includes(t)) return true
    if ((s.prompt || "").toLowerCase().includes(t)) return true
    if (s.short.toLowerCase().includes(t)) return true
    // The label is a thing you can see on the card, so it has to be searchable:
    // typing "terminal" or "obsidian" should find those sessions.
    // windowLabel only, NOT s.host: 724 cards display as Unknown while their
    // host field holds the default, so matching the field would have made
    // "cursor" return 747 cards that say Unknown. Search what is on screen.
    if ((s.windowLabel || "").toLowerCase().includes(t)) return true
    if ((s.badge || "").toLowerCase().includes(t)) return true
    if ((s.how || "").toLowerCase().includes(t)) return true
    if (convoIds && convoIds.has(s.id)) return true      // conversation hit
    return false
  }

  // Order by what you can act on, not just by age. Newest-first alone buried
  // the restorable sessions under hundreds of exited ones, so the top of the
  // list was mostly noise. Bookmarked ranks above plain Exited, so a session
  // you deliberately marked never sinks under sessions you closed on purpose.
  // Within a rank it is still newest first.
  const RANK: Record<string, number> = {
    Running: 0, Crashed: 1, Terminated: 2, Exited: 4,
  }
  const rankOf = (s: Session) => {
    const r = RANK[s.badge]
    if (r !== undefined && r < 3) return r          // Running/Crashed/Terminated
    if (s.bookmarked) return 3                       // promoted above Exited
    return r !== undefined ? r : 5
  }

  const visible = useMemo(
    () => inScope
      .filter(s => (folder === null || s.folder === folder) && matchesText(s))
      .slice()
      .sort((a, b) => data.sort_mode === "date"
        ? b.lastSeen - a.lastSeen
        : (rankOf(a) - rankOf(b) || b.lastSeen - a.lastSeen)),
    [inScope, folder, sq, convoIds, data.sort_mode])
  const selectedIn = (name: string | null) =>
    inScope.filter(s => (name === null || s.folder === name) && picked.has(s.id)).length
  const totalIn = (name: string | null) =>
    (name === null ? inScope : inScope.filter(s => s.folder === name)).length

  const toggle = (s: Session) => {
    // Live sessions would duplicate; Lost ones have nothing to resume from.
    if (s.running || s.gone) return          // nothing to resume from
    setPicked(prev => {
      const next = new Set(prev)
      next.has(s.id) ? next.delete(s.id) : next.add(s.id)
      return next
    })
  }

  // Everything currently on screen that could actually be restored. Select all
  // therefore acts per category: it follows whatever status and folder filter
  // is active, and doubles as the undo for a mistaken Clear.
  const selectable = visible.filter(
    s => !s.running && s.restorable)
  const allSelected = selectable.length > 0 && selectable.every(s => picked.has(s.id))

  const chosen = data.sessions.filter(s => picked.has(s.id))
  const windowCount = new Set(chosen.map(s => s.window)).size
  const folderCount = new Set(chosen.map(s => s.folder)).size

  const restore = async () => {
    setBusy(true)
    try {
      const r = await fetch("/api/restore" + TOKEN_Q, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: [...picked] }),
      }).then(r => r.json())
      if (r.error) { setStatus(r.error); return }   // never claim a silent success
      setStatus(`Reopened ${r.restored} session(s). Refreshing the list…`)
      setPicked(new Set())
      // NEVER shut the server down here. This single line was the cause of
      // "the dashboard dies after I restore something": clicking Restore
      // destroyed the server, so every later Refresh silently did nothing and
      // a browser reload gave ERR_CONNECTION_REFUSED. It made sense when the
      // flow ended at "restore, then close the tab"; the dashboard is now a
      // thing you come back to. Refresh the list instead, so the restored
      // sessions visibly flip to Running.
      setTimeout(reload, 1200)
    } catch (e) {
      setStatus("Restore failed: " + e)
    } finally {
      setBusy(false)
    }
  }

  const skills = data.skills.filter(s =>
    !query || s.name.toLowerCase().includes(query.toLowerCase()) ||
    (s.description || "").toLowerCase().includes(query.toLowerCase()))

  const railRow = (name: string | null, label: string) => {
    const sel = selectedIn(name)
    const total = totalIn(name)
    const active = folder === name
    return (
      <button
        key={label}
        onClick={() => setFolder(name)}
        className={cn(
          "flex w-full items-center gap-2 rounded-md border border-transparent px-2.5 py-1.5 text-left text-sm",
          "hover:bg-muted",
          active && "border-border bg-muted font-medium"
        )}
      >
        <span className="flex-1 truncate">{label}</span>
        {/* Always a circle, so folders never look inconsistent: filled when
            something is ticked, outlined when it is only a total. */}
        <span
          title={sel > 0 ? `${sel} of ${total} selected` : `${total} sessions, none selected`}
          className={cn(
            "inline-flex h-5 min-w-[20px] items-center justify-center rounded-full px-1.5 font-mono text-[11px] font-semibold",
            sel > 0
              ? "bg-primary text-primary-foreground"
              : "border border-border text-muted-foreground"
          )}
        >
          {sel > 0 ? sel : total}
        </span>
      </button>
    )
  }

  // The first read walks every project directory and tails ~700 transcripts,
  // so there is a real second or two before anything can be drawn. Show that
  // in the middle of the page rather than rendering an empty dashboard that
  // looks like "no sessions found".
  if (!loaded) {
    return (
      <div className="fixed inset-0 flex flex-col items-center justify-center gap-3">
        <div className="text-sm font-medium">Revive</div>
        <Progress className="w-56" />
        <div className="text-xs text-muted-foreground">
          Reading the session registry…
        </div>
      </div>
    )
  }

  // One definition, two homes: on the title row when there is width for it,
  // below the host line when the layout stacks.
  const controls = (
    <div className="flex flex-wrap items-center gap-1">
          <button
            onClick={reload}
            disabled={refreshing}
            title="Re-read the registry. Use this after exiting a session you just revived."
            className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-muted"
          >
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
          <button
            onClick={() => setShowSettings(v => !v)}
            title="Settings"
            className={cn(
              "flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs",
              showSettings ? "border-primary bg-primary text-primary-foreground"
                           : "border-border text-muted-foreground hover:bg-muted")}
          >
            <Settings2 className="h-3.5 w-3.5" aria-hidden />
            <span>Settings</span>
          </button>
          {(["light", "dark"] as Theme[]).map(t => (
            <button
              key={t}
              onClick={() => setTheme(t)}
              title={`${t} theme`}
              className={cn(
                "rounded-md border px-2 py-1 text-xs capitalize",
                theme === t
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border text-muted-foreground hover:bg-muted"
              )}
            >
              {t === "light" ? "Light" : "Night"}
            </button>
          ))}
    </div>
  )

  return (
    <div
      className={cn("mx-auto max-w-[1600px] px-6 pt-6",
                    wide && "overflow-hidden pb-4")}
      style={wide ? undefined : { paddingBottom: barH + 16 }}>
      {/* Sessions recorded before the hooks existed have no host, so the tool
          has to either ask or be told. This states which is in force rather
          than silently assuming Cursor, which is the bug this replaces. */}
      <header className="mb-1 flex flex-wrap items-baseline gap-3">
        <h1 className="text-xl font-semibold tracking-tight">Revive</h1>
        {/* The heading used to print sessions.length as "recoverable", but that
            total includes running sessions, which can never be restored. It
            therefore disagreed with the All-folders circle, which counts ticked
            rows. State the two quantities separately instead. */}
        {/* "recoverable" means restorable, not merely "not running": exited
            sessions are listed but cannot be brought back. */}
        <span className="text-sm text-muted-foreground">
          {inRange.filter(s => !s.running &&
            s.restorable).length} recoverable
          {inRange.some(s => s.running) &&
            `, ${inRange.filter(s => s.running).length} running`}
          {inRange.some(s => s.badge === "Exited") &&
            `, ${inRange.filter(s => s.badge === "Exited").length} exited`}
        </span>
          {wide && <div className="ml-auto">{controls}</div>}
      </header>

      {(() => {
        const unknown = data.sessions.filter(s => s.host_source === "unknown").length
        if (!data.default_host && unknown === 0) return null
        return (
          <p className="mt-4 flex flex-wrap items-center gap-x-1.5 gap-y-1
                        text-xs italic text-muted-foreground">
            {data.default_host
              ? <>All sessions with no recorded host open in
                  <span className="inline-flex items-center gap-1.5 rounded-full
                                   border px-2 py-0.5 not-italic font-mono
                                   text-[11px] text-foreground">
                    <span
                      title={conn === "ok" ? "Connected to the dashboard server"
                        : conn === "retrying" ? "Reconnecting…"
                        : "Not connected. The list below is stale."}
                      className={cn("h-1.5 w-1.5 rounded-full",
                        conn === "ok" ? "revive-dot bg-emerald-500"
                        : conn === "retrying" ? "revive-dot bg-amber-500"
                        : "bg-red-500")}
                    />
                    {data.default_host}
                  </span>
</>
              : <>{unknown} session{unknown === 1 ? "" : "s"} have no recorded host
                  and will ask before restoring.</>}
            <button className="not-italic underline underline-offset-2"
                    onClick={() => setShowSettings(true)}>Change</button>
          </p>
        )
      })()}

        {/* Stacked, there is no room beside the title, so the controls drop
            below the host line rather than wrapping into a ragged second row.
            Side by side they belong on the title row, where they have always
            been. */}
        {!wide && <div className="mt-3">{controls}</div>}


      {offline && (
        <p className="mt-3 rounded-md border border-destructive/40 px-3 py-2
                      text-xs text-destructive">
          Cannot reach the dashboard server, so what you see below is stale.
          Run <span className="font-mono">/revive</span> again in a terminal to
          restart it, then press Refresh.
        </p>
      )}

      {showSettings && (
        <div className="mt-3 rounded-lg border p-4">
          <div className="text-sm font-medium">Default host for unknown sessions</div>
          <p className="mt-1 text-xs text-muted-foreground">
            A host is recorded only while a session is running, so anything from
            before the recorder existed has none. Pick where those should reopen,
            or leave it unset to be asked each time. A session whose host WAS
            observed always wins over this.
          </p>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {["", ...data.available_hosts].map(h => (
              <button
                key={h || "ask"}
                disabled={savingHost}
                onClick={async () => {
                  // Re-fetching /api/state here cost a full rescan per click,
                  // which is the lag. The server only changes one field, and
                  // its effect on each card is knowable here, so apply it
                  // locally and let the next natural refresh confirm it.
                  setSavingHost(true)
                  setData(d => ({
                    ...d,
                    default_host: h,
                    sessions: d.sessions.map(s =>
                      s.host_source === "observed" || s.host_source === "declared"
                        ? s
                        : { ...s,
                            host: h || "unknown",
                            host_source: h ? "default" : "unknown" }),
                  }))
                  try {
                    await fetch("/api/settings" + TOKEN_Q, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({ default_host: h }),
                    })
                  } finally { setSavingHost(false) }
                }}
                className={cn("rounded-md border px-2.5 py-1 text-xs",
                  (data.default_host || "") === h
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-border text-muted-foreground hover:bg-muted")}
              >
                {h || "Ask me each time"}
              </button>
            ))}
          </div>
          {data.missing_hosts.length > 0 && (
            <div className="mt-3">
              <div className="text-xs text-muted-foreground">
                Supported but not installed here
              </div>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {data.missing_hosts.map(h => (
                  <span key={h}
                        className="rounded-md border border-dashed px-2.5 py-1
                                   text-xs text-muted-foreground opacity-60">
                    {h}
                  </span>
                ))}
              </div>
            </div>
          )}
          <p className="mt-3 text-xs text-muted-foreground">
            Evidence:{" "}
            {Object.entries(EVIDENCE).map(([kind, why]) => {
              const names = data.available_hosts
                .filter(h => data.host_evidence[h] === kind)
              if (!names.length) return null
              return (
                <span key={kind} className="mr-2">
                  <b className="font-mono">{names.join(", ")}</b>{" "}
                  <span className="opacity-70">({why})</span>;
                </span>
              )
            })}
          </p>
          <p className="mt-2 text-xs text-muted-foreground">
            The list is curated, not inferred. Probing for terminal capability
            alone also matched Blender and OrbStack, which can open a
            pseudo-terminal and are not places you would resume work. Only
            Cursor is driven end to end today; the rest bring the app forward
            and hand over the command.
          </p>

          <div className="mt-5 border-t pt-4">
            <div className="text-sm font-medium">Card order</div>
            <p className="mt-1 text-xs text-muted-foreground">
              Sorting by date alone buried the recoverable sessions under
              hundreds of exited ones, so Label leads with what you can act on.
              Either way, newer comes first within a group.
            </p>
            <div className="mt-3 flex flex-wrap gap-1.5">
              {([
                ["label", "Label", "Running, Crashed, Terminated, Bookmarked, Exited"],
                ["date", "Date", "Most recently worked on first"],
              ] as const).map(([mode, title, sub]) => (
                <button
                  key={mode}
                  title={sub}
                  onClick={async () => {
                    setData(d => ({ ...d, sort_mode: mode }))
                    try {
                      await fetch("/api/settings" + TOKEN_Q, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ sort_mode: mode }),
                      })
                    } catch { /* the next reload re-reads the truth from disk */ }
                  }}
                  className={cn("rounded-md border px-2.5 py-1 text-xs",
                    data.sort_mode === mode
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-border text-muted-foreground hover:bg-muted")}
                >
                  {title}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      <Tabs defaultValue="sessions" className="mt-5">
        <TabsList>
          <TabsTrigger value="sessions">Sessions</TabsTrigger>
          <TabsTrigger value="skills">Skills</TabsTrigger>
        </TabsList>

        <TabsContent value="sessions" className="mt-5">
          <div className="mb-5">
            <div className="flex flex-wrap items-center gap-2">
              <Input
                value={sq}
                onChange={e => setSq(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter") searchConversations() }}
                placeholder="Enter keywords to search a session"
                className="flex-1 min-w-[280px] placeholder:text-muted-foreground/50"
              />
              <Button onClick={searchConversations}
                      disabled={searching || sq.trim().length < 2}>
                {searching ? "Searching…" : "Search"}
              </Button>
              {convoIds && (
                <Button variant="ghost" onClick={() => { setConvoIds(null); setSq("") }}>
                  Clear ({convoIds.size} matched)
                </Button>
              )}
            </div>
            <p className="mt-1.5 text-xs italic text-muted-foreground/70">
              You can search a session by content, folder, name or id
            </p>
          </div>
          {/* Status filter. Nothing is hidden any more: sessions you exited on
              purpose are listed too, just filtered out of the default view. */}
          <div className="revive-chips mb-5 flex gap-2 max-sm:flex-nowrap
                          max-sm:overflow-x-auto max-sm:pb-1 sm:flex-wrap">
            {[["All", inRange.length] as [string, number],
              ["Bookmarked", inRange.filter(s => s.bookmarked).length] as [string, number],
              ...data.statusOrder
                .filter(k => statusCounts[k])
                .map(k => [k, statusCounts[k]] as [string, number])]
              .map(([label, n]) => {
                const key = label === "All" ? null : label
                const on = status_ === key
                return (
                  <button
                    key={label}
                    onClick={() => { setStatus_(key); setFolder(null) }}
                    className={cn(
                      "rounded-full border px-3 py-1 text-xs font-medium",
                      on ? "border-primary bg-primary text-primary-foreground"
                         : "border-border text-muted-foreground hover:bg-muted"
                    )}
                  >
                    {label}
                    <span className="ml-1.5 font-mono opacity-70">{n}</span>
                  </button>
                )
              })}
          </div>
          <div className="revive-chips mb-5 flex items-center gap-2
                          max-sm:flex-nowrap max-sm:overflow-x-auto max-sm:pb-1
                          sm:flex-wrap">
            <span className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              Last
            </span>
            {RANGES.map(r => {
              const on = range === r.label
              const from = r.from(), to = r.to ? r.to() : Infinity
              const n = data.sessions.filter(s => {
                const t = s.lastSeen || 0
                return t >= from && t < to
              }).length
              const label = r.label
              return (
                <button
                  key={label}
                  title={r.hint}
                  onClick={() => { setRange(r.label); setFolder(null) }}
                  className={cn(
                    "rounded-full border px-3 py-1 text-xs font-medium",
                    on ? "border-primary bg-primary text-primary-foreground"
                       : "border-border text-muted-foreground hover:bg-muted"
                  )}
                >
                  {label}
                  <span className="ml-1.5 font-mono opacity-70">{n}</span>
                </button>
              )
            })}
          </div>
          {/* Freeze-pane layout: the header, filters and search above stay put,
              and each column below scrolls on its own. With 790 cards the page
              scroll carried the folder rail away, so choosing a different
              folder meant scrolling back to the top first.

              Heights are viewport-relative, not fixed, and the two panes only
              become independent from md upwards. Below that the grid collapses
              to one column and the rail sits ABOVE the cards, where a tall
              independent scroller would be worse than useless: the folder list
              is capped instead, and the cards use the page scroll as normal. */}
          {/* Same freeze-pane idea in both layouts, just rotated. Wide: two
              columns, each scrolling on its own. Narrow: the folder rail is
              pinned at a capped height and the cards take the rest and scroll
              under it. Letting the PAGE scroll on narrow screens was wrong: it
              carried the rail off the top, so choosing a folder meant scrolling
              back up, which is the thing this exists to prevent. */}
          {/* Narrow screens let the PAGE scroll. Clipping it, and giving this
              container a measured height, is what starved the card list: with
              the filters stacked the header ate most of the viewport, the
              measurement bottomed out at its 240px floor, and the folder rail
              then took all of that, leaving the cards nothing. */}
          <div ref={panesRef}
               style={wide && paneH ? { height: paneH } : undefined}
               className={cn("gap-6",
                 wide ? "grid grid-cols-1 items-start md:grid-cols-[210px_1fr]"
                      : "flex flex-col")}>
            {/* Narrow: the rail STICKS to the top of the viewport, so it stays
                on screen while the cards scroll under it, which is the freeze
                pane behaviour asked for. Capping it inside a fixed-height pane
                is what let it swallow the pane whole. */}
            <nav
              style={wide && paneH ? { maxHeight: paneH } : undefined}
              className={cn("overflow-y-auto pr-1",
                !wide && "sticky top-0 z-20 max-h-[30vh] shrink-0 border-b " +
                         "border-border bg-background pb-2")}>
              <h2 className="sticky top-0 z-10 mb-2 bg-background pb-1 text-[11px]
                             font-semibold uppercase tracking-wider text-muted-foreground">
                Folders
              </h2>
              {railRow(null, "All folders")}
              <Separator className="my-2" />
              {data.folders
                .filter(f => inScope.some(s => s.folder === f.name))
                .map(f => railRow(f.name, f.name))}
            </nav>

            {/* Two columns from 1024px, three from 1536px. The cards are dense
                (prompt excerpt, path, full id), so a third column only earns its
                place on a genuinely wide screen; below that it would just
                truncate everything. */}
            <div
              style={wide && paneH ? { maxHeight: paneH } : undefined}
              className={cn("grid grid-cols-1 gap-3 pr-1",
                            "lg:grid-cols-2 2xl:grid-cols-3",
                            // Wide: its own scroller beside the rail. Narrow:
                            // no scroller of its own, it rides the page scroll
                            // beneath the stuck rail and can never be squeezed.
                            wide ? "overflow-y-auto" : "pt-3")}>
              {visible.map(s => {
                const on = picked.has(s.id)
                return (
                  <Card
                    key={s.id}
                    onClick={() => toggle(s)}
                    title={s.running ? "Still running, cannot be restored" : s.cwd}
                    className={cn(
                      "cursor-pointer transition-colors",
                      on && "ring-1 ring-foreground/60",
                      (s.running || s.gone) && "cursor-default opacity-60"
                    )}
                  >
                    <CardContent className="p-4">
                      <div className="mb-2.5 flex items-center gap-2.5">
                        <Checkbox checked={on} disabled={s.running || s.gone}
                                  aria-label="Open session" />
                        <span className="flex-1 text-xs text-muted-foreground">
                          {s.isCurrent ? "This session"
                            : s.running ? "Still open"
                            : s.gone ? "Transcript gone"
                            : "Open session"}
                        </span>
                        <Badge className={cn("rounded-full border-transparent", BADGE[s.state])}>
                          {s.badge}
                        </Badge>
                      </div>
                      <div className="font-mono text-xs text-muted-foreground">
                        {s.predecessor && (
                          <span title="this session continues from that one">
                            {s.predecessor} &rarr;{" "}
                          </span>
                        )}
                        {s.predecessor || s.successor ? s.short : null}
                        {s.successor && (
                          <span title="cleared, and continued as that one">
                            {" "}&rarr; {s.successor}
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 font-semibold">
                        {s.sessionName || s.folder}
                      </div>
                      <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
                        {s.prompt || <i>no recorded prompt</i>}
                      </p>
                      <Separator className="my-2.5" />
                      <div className="flex items-center gap-1.5 truncate font-mono text-xs text-muted-foreground">
                        <span>
                          {s.ago} ago{s.lifetime && ` \u00b7 lived ${s.lifetime}`}
                          {s.how && ` \u00b7 ${s.how}`}
                          {s.gone && " \u00b7 transcript gone"} &middot; {s.windowLabel}
                        </span>
                      </div>
                      <div className="truncate font-mono text-xs text-muted-foreground">
                        {shortPath(s.cwd)}
                      </div>
                      {/* Full id, last: you need all of it to paste into
                          `claude --resume`, and the 8-char form never was. */}
                      {/* Bottom-right, on the id row: out of the reading path
                          of the title and state, still the corner people reach
                          for. Stops propagation so starring never ticks the row. */}
                      <div className="mt-1 flex items-end gap-2">
                        <div className="min-w-0 flex-1 select-all break-all
                                        font-mono text-[11px] text-muted-foreground/60">
                          {s.id}
                        </div>
                        <Toggle
                          pressed={s.bookmarked}
                          onPressedChange={v => toggleBookmark(s.id, v)}
                          title={s.bookmarked ? "Remove bookmark" : "Bookmark this session"}
                          className={cn("-mb-1 -mr-1 shrink-0",
                                        s.bookmarked && "text-amber-500")}
                        >
                          <Bookmark className="h-4 w-4"
                                    fill={s.bookmarked ? "currentColor" : "none"} />
                        </Toggle>
                      </div>
                    </CardContent>
                  </Card>
                )
              })}
              {visible.length === 0 && (
                <p className="py-12 text-center text-muted-foreground">Nothing to revive.</p>
              )}
            </div>
          </div>
        </TabsContent>

        <TabsContent value="skills" className="mt-5">
          <Input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder={`Search ${data.skills.length} skills`}
            className="mb-4"
          />
          {skills.map(s => (
            <div key={s.name} className="border-b py-3">
              <b className="font-mono font-semibold">{s.name}</b>
              <p className="mt-0.5 text-sm text-muted-foreground">{s.description}</p>
            </div>
          ))}
          {skills.length === 0 && (
            <p className="py-12 text-center text-muted-foreground">No matching skills.</p>
          )}
        </TabsContent>
      </Tabs>

      <div ref={barRef}
           className="fixed inset-x-0 bottom-0 flex items-center gap-3 border-t bg-background/90 px-6 py-3 backdrop-blur">
        <span className="mr-auto text-sm text-muted-foreground">
          {status
            ? status
            : picked.size === 0
              ? `${inRange.filter(s => !s.running &&
                   s.restorable).length} recoverable across ${
                   new Set(inScope.map(s => s.folder)).size} folders`
              : `${picked.size} selected across ${folderCount} folder${folderCount === 1 ? "" : "s"}, restoring into ${windowCount} window${windowCount === 1 ? "" : "s"}`}
        </span>
        {selectable.length > 1 && !allSelected && (
          <Button
            variant="outline"
            onClick={() => setPicked(prev =>
              new Set([...prev, ...selectable.map(s => s.id)]))}
          >
            Select all {selectable.length}
          </Button>
        )}
        <Button variant="outline" onClick={() => setPicked(new Set())}>Clear</Button>
        <Button onClick={restore} disabled={picked.size === 0 || busy}>
          {busy ? "Restoring" : picked.size === 0 ? "Restore" : `Restore ${picked.size}`}
        </Button>
      </div>
    </div>
  )
}
