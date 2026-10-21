"""Builds a static GitHub Pages version of the challenge into docs/.

The live apps are Python servers; this renders every page they can serve
ahead of time and swaps the server-side pieces for browser-side equivalents:

  - Links are rewritten to relative paths so the site works under
    https://<owner>.github.io/<repo>/ as well as from a local static server.
  - Comment / assign API calls are answered from localStorage, so changes
    persist per browser only.
  - Ownership lookups run in JavaScript against an embedded copy of the
    team registry.
  - Dates are shifted forward by the days elapsed since the build, keeping
    SLO deadlines relative to "now" like the live app.

Usage (from the repo root):
  python3 scripts/build_pages.py
"""

from datetime import datetime
from datetime import timezone
import html
import json
import os
import re
import shutil
import sys

sys.dont_write_bytecode = True  # Keep the committed __pycache__ untouched.

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from apps.buganizer import controller as tracker
from apps.team_directory.controller import render_team_directory_html
from apps.team_directory.data import get_all_teams

OUT_DIR = os.path.join(PROJECT_ROOT, "docs")
BUILD_TIME = datetime.now(timezone.utc)

# Rebuild the bug registry against a single, known build timestamp.
tracker.ISSUES.clear()
tracker.ISSUES.update(tracker.build_issues(BUILD_TIME.replace(tzinfo=None)))

QUEUE_ID = "5829104"
BACKLOG_ID = "7920155"


def write(rel_path: str, content: str):
  path = os.path.join(OUT_DIR, rel_path)
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w", encoding="utf-8") as f:
    f.write(content)


def js_json(data) -> str:
  """JSON that is safe to inline inside a <script> tag."""
  return json.dumps(data).replace("</", "<\\/")


def relink(page: str, root: str) -> str:
  """Rewrites the apps' absolute server URLs into site-relative links."""
  rules = [
      (r'"/static/buganizer\.css"', f'"{root}tracker/static/buganizer.css"'),
      (r'"/static/team_directory\.css"', f'"{root}directory/static/team_directory.css"'),
      (r'"/hotlists/(\d+)"', rf'"{root}tracker/hotlists/\1/"'),
      (r'"/hotlists"', f'"{root}tracker/hotlists/{QUEUE_ID}/"'),
      (r'"/issues/(\d+)"', rf'"{root}tracker/issues/\1/"'),
      (r'"http://localhost:8081/hotlists/(\d+)"', rf'"{root}tracker/hotlists/\1/"'),
      (r'"http://localhost:8082/?"', f'"{root}directory/"'),
  ]
  for pattern, repl in rules:
    page = re.sub(pattern, repl, page)

  leftovers = re.findall(r'(?:href|action)="/[^"]*"|localhost:\d+', page)
  if leftovers:
    raise RuntimeError(f"Unrewritten server links: {sorted(set(leftovers))}")
  return page


def inject(page: str, marker: str, snippet: str) -> str:
  """Inserts snippet right before marker, failing loudly if a template changed."""
  if page.count(marker) != 1:
    raise RuntimeError(f"Expected exactly one {marker!r} in template")
  return page.replace(marker, snippet + marker)


def demo_badge(root: str) -> str:
  return f"""
    <div class="static-demo-badge" style="position:fixed;left:12px;bottom:12px;z-index:50;display:flex;gap:10px;align-items:center;padding:6px 12px;border-radius:999px;background:#202124;color:#fff;font:500 12px/1.4 Inter,system-ui,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.25);">
        <span>Static demo &middot; changes are saved in this browser only</span>
        <a href="#" onclick="try{{localStorage.removeItem('pi2-static-tracker')}}catch(e){{}};location.reload();return false;" style="color:#8ab4f8;">Reset</a>
        <a href="{root}" style="color:#8ab4f8;">Docs</a>
    </div>
"""


TRACKER_SHIM = r"""
    <script>
    // Static-site stand-in for the Issue Tracker server (see scripts/build_pages.py).
    (function () {
        const BUILD_TIME = __BUILD_MS__;
        const PAGE = __PAGE__;
        const KEY = 'pi2-static-tracker';
        const DAY = 86400000;
        const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                        'August', 'September', 'October', 'November', 'December'];
        const DATE_RE = new RegExp('\\b(' + MONTHS.join('|') + ') (\\d{2}), (\\d{4})\\b', 'g');
        const dayShift = Math.floor(Date.now() / DAY) - Math.floor(BUILD_TIME / DAY);

        const pad = (n) => String(n).padStart(2, '0');
        const fmtDate = (d) => `${MONTHS[d.getUTCMonth()]} ${pad(d.getUTCDate())}, ${d.getUTCFullYear()}`;
        const nowStr = () => {
            const d = new Date();
            return `${fmtDate(d)} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
        };
        const esc = (s) => String(s).replace(/[&<>"']/g, (ch) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#x27;' }[ch]));

        function load() {
            try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
        }
        function save(state) {
            try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
        }

        // Keep build-time dates relative to today, as the live server does at startup.
        function shiftDates() {
            if (dayShift <= 0) return;
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            const nodes = [];
            while (walker.nextNode()) nodes.push(walker.currentNode);
            nodes.forEach((n) => {
                const shifted = n.nodeValue.replace(DATE_RE, (m, mon, day, year) =>
                    fmtDate(new Date(Date.UTC(+year, MONTHS.indexOf(mon), +day + dayShift))));
                if (shifted !== n.nodeValue) n.nodeValue = shifted;
            });
        }

        // Mirrors render_comments_html() in apps/buganizer/controller.py.
        function commentCard(c) {
            const rawAuthor = c.author.split('@')[0].split('(')[0].trim();
            let initials = rawAuthor.split(/\s+/).filter(Boolean).slice(0, 2)
                .map((part) => part[0].toUpperCase()).join('');
            if (!initials) initials = rawAuthor.slice(0, 2).toUpperCase();
            let sum = 0;
            for (const ch of rawAuthor) sum += ch.codePointAt(0);
            const tone = sum % 6 + 1;
            return `
    <div class="card bug-card comment-card">
        <div class="card-header">
            <div class="reporter-info">
                <div class="avatar tone-${tone}">${esc(initials)}</div>
                <div class="reporter-details">
                    <span class="reporter-name"><strong>${esc(c.author)}</strong></span>
                    <span class="report-date"><span class="comment-number">${esc(c.number)}</span> &bull; commented on ${esc(c.date)}</span>
                </div>
            </div>
            <div class="card-actions">
                <i class="material-icons">reply</i>
                <i class="material-icons">more_vert</i>
            </div>
        </div>
        <div class="card-body description-content">
            ${esc(c.text).replace(/\n/g, '<br>')}
        </div>
    </div>`;
        }

        function applyToIssuePage(st) {
            const comments = st.comments || [];
            const container = document.getElementById('commentsContainer');
            comments.forEach((c) => container.insertAdjacentHTML('beforeend', commentCard(c)));
            const count = document.querySelector('.section-heading .count');
            if (count) count.textContent = PAGE.baseComments + comments.length;
            if (st.assignee !== undefined) {
                document.getElementById('assigneeInput').value = st.assignee;
                document.querySelectorAll('.status-chip').forEach((chip) => {
                    chip.className = chip.className.replace(/status-\S+/, 'status-assigned');
                });
                document.getElementById('statusText').textContent = 'Assigned';
                const headerChip = document.querySelector('.bug-id-row .status-chip');
                if (headerChip) headerChip.textContent = 'Assigned';
            }
            if (st.modified) document.getElementById('lastModifiedDate').textContent = st.modified;
        }

        function applyToHotlistRow(row, st) {
            const cells = row.children;
            if (st.assignee !== undefined) {
                row.dataset.assignee = (st.assignee || 'unassigned').toLowerCase();
                row.dataset.status = 'assigned';
                cells[4].innerHTML = '<span class="status-chip status-assigned">Assigned</span>';
                cells[6].innerHTML = st.assignee
                    ? `<span class='assignee-chip'><span class='mini-avatar'>${esc(st.assignee.slice(0, 2))}</span>@${esc(st.assignee)}</span>`
                    : "<span class='assignee-chip unassigned'>Unassigned</span>";
            }
            const extra = (st.comments || []).map((c) => `${c.author} ${c.text} ${c.number}`).join(' ');
            row.dataset.search = `${row.dataset.search} ${st.assignee || ''} ${extra}`.toLowerCase();
            if (st.modified) cells[7].textContent = st.modified;
        }

        // Answer the issue page's POST calls from localStorage instead of the server.
        const realFetch = window.fetch.bind(window);
        window.fetch = function (url, opts) {
            const m = String(url).match(/^\/api\/issues\/(\d+)\/(comments|assign)$/);
            if (!m) return realFetch(url, opts);
            const body = JSON.parse((opts && opts.body) || '{}');
            const state = load();
            const st = state[m[1]] = state[m[1]] || { comments: [] };
            const now = nowStr();
            let result;
            if (m[2] === 'comments') {
                const number = PAGE.baseComments + st.comments.length + 1;
                const comment = {
                    number: `#${number}`,
                    author: body.author || 'security-triage@company.com',
                    date: now,
                    text: (body.text || '').trim(),
                };
                st.comments.push(comment);
                result = { success: true, comment: comment, total_comments: number };
            } else {
                const assignee = (body.assignee_ldap || '').trim().replace(/^@+/, '');
                st.assignee = assignee;
                result = { success: true, bug_id: m[1], assignee_ldap: assignee, status: 'Assigned' };
            }
            st.modified = now;
            save(state);
            return Promise.resolve(new Response(JSON.stringify(result), {
                status: 200, headers: { 'Content-Type': 'application/json' },
            }));
        };

        shiftDates();
        const state = load();
        if (PAGE.kind === 'issue' && state[PAGE.id]) {
            applyToIssuePage(state[PAGE.id]);
        } else if (PAGE.kind === 'hotlist') {
            document.querySelectorAll('#issuesTableBody tr[data-id]').forEach((row) => {
                if (state[row.dataset.id]) applyToHotlistRow(row, state[row.dataset.id]);
            });
            // The page already filtered on load; redo it now that rows reflect local changes.
            const input = document.getElementById('hotlistFilterInput');
            if (input && input.value && typeof filterTable === 'function') filterTable(input.value);
        }
    })();
    </script>
"""

# Hotlist search: the server pre-filled the input from ?q=; read it in the browser instead.
HOTLIST_QUERY_FROM_URL = """
        const initialQuery = new URLSearchParams(window.location.search).get('q');
        if (filterInput && initialQuery) {
            filterInput.value = initialQuery;
        }

"""

DIRECTORY_SHIM = r"""
    <script>
    // Static-site stand-in for GET /api/ownership (see scripts/build_pages.py).
    (function () {
        const TEAMS = __TEAMS__;

        // Mirrors resolve_ownership() in apps/team_directory/data.py.
        function resolveOwnership(pathOrEndpoint) {
            if (!pathOrEndpoint) return null;
            const target = pathOrEndpoint.trim().toLowerCase();
            let bestMatch = null;
            let bestScore = 0;
            for (const team of TEAMS) {
                let score = 0;
                for (const p of team.owned_paths) {
                    const pClean = p.replace('//depot/', '').replace('/...', '').toLowerCase();
                    if (p.toLowerCase().includes(target) || target.includes(pClean) || pClean.includes(target)) {
                        score = Math.max(score, 100);
                    }
                }
                for (const ep of team.owned_endpoints) {
                    const epClean = ep.replace('/*', '').toLowerCase();
                    if (target === epClean || (epClean && target.includes(epClean))) {
                        score = Math.max(score, 90);
                    }
                }
                if (score > bestScore) {
                    bestScore = score;
                    bestMatch = team;
                }
            }
            return bestMatch;
        }

        const realFetch = window.fetch.bind(window);
        window.fetch = function (url, opts) {
            const u = new URL(String(url), window.location.href);
            if (!String(url).startsWith('/api/ownership')) return realFetch(url, opts);
            const target = u.searchParams.get('path') || u.searchParams.get('endpoint') || '';
            const team = resolveOwnership(target);
            const result = team ? {
                matched: true,
                query: target,
                team: team,
                broad_component: team.broad_component || '',
                broad_component_filter: `component:"${team.broad_component || ''}"`,
                suggested_assignee_ldap: team.primary_oncall_ldap,
                suggested_assignee_name: team.primary_oncall_name,
                tech_lead_ldap: team.tl_ldap,
                escalation_alias: team.email,
            } : { matched: false, query: target, message: 'No direct ownership match found' };
            return Promise.resolve(new Response(JSON.stringify(result), {
                status: 200, headers: { 'Content-Type': 'application/json' },
            }));
        };
    })();
    </script>
"""


def tracker_page(page: str, root: str, page_info: dict) -> str:
  shim = TRACKER_SHIM.replace(
      "__BUILD_MS__", str(int(BUILD_TIME.timestamp() * 1000))
  ).replace("__PAGE__", js_json(page_info))
  page = inject(page, "</body>", shim + demo_badge(root))
  return relink(page, root)


def build_tracker():
  css_src = os.path.join(PROJECT_ROOT, "apps", "buganizer", "static", "buganizer.css")
  os.makedirs(os.path.join(OUT_DIR, "tracker", "static"), exist_ok=True)
  shutil.copy(css_src, os.path.join(OUT_DIR, "tracker", "static", "buganizer.css"))

  for hotlist_id in tracker.HOTLISTS:
    page = tracker.render_hotlist_html(hotlist_id)
    page = inject(page, "        // Initialize state on load", HOTLIST_QUERY_FROM_URL)
    page = tracker_page(page, "../../../", {"kind": "hotlist", "id": hotlist_id})
    write(f"tracker/hotlists/{hotlist_id}/index.html", page)

  for issue_id, issue in tracker.ISSUES.items():
    page = tracker.render_issue_html(issue_id)
    info = {
        "kind": "issue",
        "id": issue_id,
        "baseComments": len(issue.get("comments", [])),
    }
    page = tracker_page(page, "../../../", info)
    write(f"tracker/issues/{issue_id}/index.html", page)

  write("tracker/index.html", redirect_page(f"hotlists/{QUEUE_ID}/"))
  write("tracker/hotlists/index.html", redirect_page(f"{QUEUE_ID}/"))
  write("tracker/issues/index.html", redirect_page(f"../hotlists/{QUEUE_ID}/"))


def build_directory():
  css_src = os.path.join(
      PROJECT_ROOT, "apps", "team_directory", "static", "team_directory.css"
  )
  os.makedirs(os.path.join(OUT_DIR, "directory", "static"), exist_ok=True)
  shutil.copy(css_src, os.path.join(OUT_DIR, "directory", "static", "team_directory.css"))

  page = render_team_directory_html()
  shim = DIRECTORY_SHIM.replace("__TEAMS__", js_json(get_all_teams()))
  page = inject(page, "</body>", shim)
  write("directory/index.html", relink(page, "../"))


def redirect_page(target: str) -> str:
  return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="0; url={target}">
    <title>Redirecting…</title>
</head>
<body>
    <a href="{target}">Continue</a>
</body>
</html>
"""


# --- Documentation pages ---------------------------------------------------

DOC_PAGES = [
    # (output file, source markdown, nav label)
    ("readme.html", "README.md", "Challenge README"),
    ("guidelines.html", "TRIAGE_GUIDELINES.md", "Triage Guidelines"),
]


def localize_markdown(text: str) -> str:
  """Points the docs' localhost URLs and file links at the static site."""

  def to_site_path(url: str) -> str:
    m = re.fullmatch(r"http://localhost:8081/(hotlists|issues)/(\d+)/?", url)
    if m:
      return f"tracker/{m.group(1)}/{m.group(2)}/"
    if re.fullmatch(r"http://localhost:8081/?", url):
      return "tracker/"
    if re.fullmatch(r"http://localhost:8082/?", url):
      return "directory/"
    raise RuntimeError(f"No static equivalent for {url}")

  # [text](http://localhost...) -> [text](site path)
  text = re.sub(
      r"\]\((http://localhost:\d+[^)\s]*)\)",
      lambda m: f"]({to_site_path(m.group(1))})",
      text,
  )
  # `http://localhost...` in code spans -> clickable link
  text = re.sub(
      r"`(http://localhost:\d+[^`\s]*)`",
      lambda m: f"[`{to_site_path(m.group(1))}`]({to_site_path(m.group(1))})",
      text,
  )
  for page, src, _ in DOC_PAGES:
    text = re.sub(rf"\]\((?:\./)?{re.escape(src)}(#[^)]*)?\)", rf"]({page}\1)", text)

  if "localhost:" in text:
    raise RuntimeError("Unrewritten localhost reference left in markdown")
  return text


SITE_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/material-icons@1.13.12/iconfont/material-icons.min.css" rel="stylesheet">
    <link rel="stylesheet" href="assets/site.css">
</head>
<body>
    <header class="site-bar">
        <a href="./" class="site-brand"><i class="material-icons">shield</i> Incident Triage Challenge</a>
        <nav class="site-nav">
            <a href="readme.html" class="{readme_active}">README</a>
            <a href="guidelines.html" class="{guidelines_active}">Triage Guidelines</a>
            <a href="tracker/hotlists/5829104/">Issue Tracker</a>
            <a href="directory/">Team Directory</a>
        </nav>
    </header>
"""


def site_head(title: str, active: str = "") -> str:
  return SITE_HEAD.format(
      title=html.escape(title),
      readme_active="active" if active == "readme.html" else "",
      guidelines_active="active" if active == "guidelines.html" else "",
  )


def build_doc_page(out_name: str, src: str, label: str):
  with open(os.path.join(PROJECT_ROOT, src), "r", encoding="utf-8") as f:
    markdown = localize_markdown(f.read())

  note = ""
  if src == "README.md":
    note = """
        <div class="static-note">
            <i class="material-icons">info</i>
            <div>This is the static GitHub Pages edition: the links below open browser-only copies of the
            Issue Tracker and Team Directory. Comments and assignments are saved in your browser only.
            To run the original servers, follow <em>Getting Started</em>.</div>
        </div>"""

  page = site_head(f"{label} · Incident Triage Challenge", out_name) + f"""
    <main class="doc">{note}
        <article id="content" class="markdown"></article>
    </main>
    <script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
    <script>
        const SOURCE = {js_json(markdown)};
        document.getElementById('content').innerHTML = marked.parse(SOURCE);
    </script>
</body>
</html>
"""
  write(out_name, page)


def build_index():
  cards = [
      ("readme.html", "description", "Challenge README",
       "Setup, hotlist overview, objectives and tasks."),
      ("guidelines.html", "rule", "Triage Guidelines",
       "Routing, follow-up and escalation rules for security findings."),
      (f"tracker/hotlists/{QUEUE_ID}/", "bug_report", "Issue Tracker",
       "Active triage queue (hotlist:5829104) and the enterprise backlog (hotlist:7920155)."),
      ("directory/", "corporate_fare", "Team & Ownership Directory",
       "Teams, owned endpoints, Tech Leads and on-call rosters."),
  ]
  cards_html = "\n".join(
      f"""            <a class="tile" href="{href}">
                <i class="material-icons">{icon}</i>
                <strong>{html.escape(title)}</strong>
                <span>{html.escape(desc)}</span>
            </a>"""
      for href, icon, title, desc in cards
  )
  page = site_head("Incident Triage Challenge") + f"""
    <main class="landing">
        <h1>Incident Triage &amp; Bug Management Challenge</h1>
        <p class="lede">Start with the README, keep the triage guidelines open, then work the tickets in the Issue Tracker.</p>
        <div class="tiles">
{cards_html}
        </div>
        <p class="fine-print">Static edition built {BUILD_TIME.strftime("%B %d, %Y")}. Bug dates stay relative to today.</p>
    </main>
</body>
</html>
"""
  write("index.html", page)


SITE_CSS = """:root {
    --text: #202124;
    --muted: #5f6368;
    --line: #dadce0;
    --bg: #f8f9fa;
    --card: #ffffff;
    --accent: #1a73e8;
    --code-bg: #f1f3f4;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 15px/1.6 Inter, system-ui, sans-serif;
}
a { color: var(--accent); }
.site-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 24px;
    align-items: center;
    padding: 12px 24px;
    background: var(--card);
    border-bottom: 1px solid var(--line);
}
.site-brand {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 600;
    color: var(--text);
    text-decoration: none;
}
.site-brand .material-icons { color: var(--accent); }
.site-nav { display: flex; flex-wrap: wrap; gap: 4px; }
.site-nav a {
    padding: 6px 12px;
    border-radius: 6px;
    color: var(--muted);
    text-decoration: none;
    font-weight: 500;
}
.site-nav a:hover { background: var(--code-bg); }
.site-nav a.active { background: #e8f0fe; color: var(--accent); }

.landing, .doc { max-width: 920px; margin: 0 auto; padding: 40px 16px 80px; }
.landing h1 { font-size: 30px; line-height: 1.25; margin: 0 0 8px; }
.lede { color: var(--muted); margin: 0 0 28px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }
.tile {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 20px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    color: var(--text);
    text-decoration: none;
}
.tile:hover { border-color: var(--accent); box-shadow: 0 1px 6px rgba(26, 115, 232, .15); }
.tile .material-icons { color: var(--accent); font-size: 28px; }
.tile span { color: var(--muted); font-size: 14px; }
.fine-print { margin-top: 32px; color: var(--muted); font-size: 13px; }

.static-note {
    display: flex;
    gap: 10px;
    padding: 12px 16px;
    margin-bottom: 24px;
    background: #e8f0fe;
    border-radius: 8px;
    font-size: 14px;
}
.static-note .material-icons { color: var(--accent); }
.markdown {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 8px 32px 32px;
    overflow-wrap: anywhere;
}
.markdown h1, .markdown h2 { border-bottom: 1px solid var(--line); padding-bottom: 6px; }
.markdown h1 { font-size: 26px; }
.markdown h2 { font-size: 21px; margin-top: 36px; }
.markdown h3 { font-size: 17px; margin-top: 28px; }
.markdown code {
    font-family: "JetBrains Mono", monospace;
    font-size: 13px;
    background: var(--code-bg);
    padding: 1px 5px;
    border-radius: 4px;
}
.markdown pre { background: var(--code-bg); padding: 14px 16px; border-radius: 8px; overflow-x: auto; }
.markdown pre code { padding: 0; background: none; }
.markdown table { border-collapse: collapse; display: block; overflow-x: auto; }
.markdown th, .markdown td { border: 1px solid var(--line); padding: 6px 12px; text-align: left; }
.markdown blockquote { margin: 0; padding: 0 16px; border-left: 4px solid var(--line); color: var(--muted); }

@media (max-width: 600px) {
    .markdown { padding: 4px 16px 20px; }
    .site-bar { padding: 12px 16px; }
}
"""


def main():
  if os.path.isdir(OUT_DIR):
    shutil.rmtree(OUT_DIR)
  os.makedirs(OUT_DIR)

  build_tracker()
  build_directory()
  for out_name, src, label in DOC_PAGES:
    build_doc_page(out_name, src, label)
  build_index()
  write("assets/site.css", SITE_CSS)
  # Serve files as-is; nothing here needs Jekyll.
  write(".nojekyll", "")

  print(f"Static site written to {os.path.relpath(OUT_DIR, PROJECT_ROOT)}/")


if __name__ == "__main__":
  main()
