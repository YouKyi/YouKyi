#!/usr/bin/env python3
"""Generate a violet terminal-style GitHub stats card (assets/stats.svg).

Token resolution: GH_TOKEN (a personal access token, optional) is preferred so
that private contributions and all-time commits can be counted; otherwise it
falls back to GITHUB_TOKEN (public data only). With no token it writes a
placeholder card (dashes); with --sample it writes representative numbers.
No third-party dependencies: standard library only.

Languages / stars / repo count are always computed from PUBLIC repos only
(privacy:PUBLIC), so a PAT never leaks private project details onto the card.
Only the aggregate commit count includes private contributions (when a PAT is
provided).
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.request
from xml.sax.saxutils import escape

GRAPHQL = "https://api.github.com/graphql"

USER_QUERY = """
query($login:String!){
  user(login:$login){
    login
    createdAt
    followers{ totalCount }
    pullRequests{ totalCount }
    issues{ totalCount }
    repositories(first:100, ownerAffiliations:OWNER, isFork:false, privacy:PUBLIC,
                 orderBy:{field:STARGAZERS, direction:DESC}){
      totalCount
      nodes{
        stargazerCount
        languages(first:10, orderBy:{field:SIZE, direction:DESC}){
          edges{ size node{ name } }
        }
      }
    }
  }
}
"""


def _post(token, query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GRAPHQL,
        data=body,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "stats-card",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.load(r)
    if "errors" in payload:
        raise RuntimeError(payload["errors"])
    return payload["data"]


def all_time_commits(token, login, start_year):
    """Sum totalCommitContributions over every year since account creation.
    Includes private commits when `token` is a PAT able to see them."""
    now = datetime.datetime.now(datetime.timezone.utc)
    this_year = now.year
    years = range(start_year, this_year + 1)

    def upto(y):
        if y == this_year:
            return now.strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{y}-12-31T23:59:59Z"

    aliases = " ".join(
        f'y{y}: contributionsCollection(from:"{y}-01-01T00:00:00Z", '
        f'to:"{upto(y)}"){{ totalCommitContributions }}'
        for y in years
    )
    q = "query($login:String!){ user(login:$login){ " + aliases + " } }"
    u = _post(token, q, {"login": login})["user"]
    return sum(u[f"y{y}"]["totalCommitContributions"] for y in years)


def _rest(token, url):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "stats-card",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return urllib.request.urlopen(req, timeout=30)


def list_repos(token, login):
    """Non-fork repos the user owns OR can reach via org membership / collaboration
    (private + org repos included when token is a PAT with repo + read:org).
    Deduplicated across affiliations."""
    repos, seen, cursor = [], set(), None
    q = ("query($login:String!,$cursor:String){ user(login:$login){ "
         "repositories(first:100, "
         "ownerAffiliations:[OWNER,ORGANIZATION_MEMBER,COLLABORATOR], "
         "isFork:false, after:$cursor){ "
         "pageInfo{ hasNextPage endCursor } nodes{ name owner{ login } } } } }")
    while True:
        page = _post(token, q, {"login": login, "cursor": cursor})["user"]["repositories"]
        for n in page["nodes"]:
            key = (n["owner"]["login"], n["name"])
            if key not in seen:
                seen.add(key)
                repos.append(key)
        if not page["pageInfo"]["hasNextPage"]:
            return repos
        cursor = page["pageInfo"]["endCursor"]


def repo_loc(token, owner, name, login):
    """User's additions/deletions in one repo via the contributor-stats endpoint."""
    url = f"https://api.github.com/repos/{owner}/{name}/stats/contributors"
    try:
        for _ in range(5):
            resp = _rest(token, url)
            if resp.status == 202:  # GitHub is still computing the stats
                time.sleep(3)
                continue
            data = json.load(resp)
            if not data:
                return (0, 0)
            add = dele = 0
            for c in data:
                author = c.get("author") or {}
                if (author.get("login") or "").lower() == login.lower():
                    for w in c["weeks"]:
                        add += w["a"]
                        dele += w["d"]
            return (add, dele)
    except Exception as exc:
        print(f"warning: LOC for {owner}/{name} failed ({exc})", file=sys.stderr)
    return (0, 0)


def lines_of_code(token, login):
    """Aggregate additions / deletions / net across all owned repos."""
    add = dele = 0
    for owner, name in list_repos(token, login):
        a, d = repo_loc(token, owner, name, login)
        add += a
        dele += d
    return add, dele, add - dele


def fetch(token, login):
    u = _post(token, USER_QUERY, {"login": login})["user"]
    repos = u["repositories"]["nodes"]
    stars = sum(n["stargazerCount"] for n in repos)
    langs = {}
    for n in repos:
        for e in n["languages"]["edges"]:
            langs[e["node"]["name"]] = langs.get(e["node"]["name"], 0) + e["size"]
    top = [name for name, _ in sorted(langs.items(), key=lambda kv: kv[1], reverse=True)[:5]]
    start_year = int(u["createdAt"][:4])
    try:
        commits = all_time_commits(token, login, start_year)
    except Exception as exc:  # fall back to last-year commits so the card still builds
        print(f"warning: all-time commits failed ({exc}); using last year", file=sys.stderr)
        lastyear = _post(
            token,
            "query($login:String!){ user(login:$login){ contributionsCollection{ totalCommitContributions } } }",
            {"login": login},
        )
        commits = lastyear["user"]["contributionsCollection"]["totalCommitContributions"]
    try:
        loc_add, loc_del, loc_net = lines_of_code(token, login)
    except Exception as exc:  # never let LOC break the whole card
        print(f"warning: lines of code failed ({exc})", file=sys.stderr)
        loc_add = loc_del = loc_net = None
    return {
        "login": u["login"],
        "commits": commits,
        "stars": stars,
        "prs": u["pullRequests"]["totalCount"],
        "issues": u["issues"]["totalCount"],
        "repos": u["repositories"]["totalCount"],
        "followers": u["followers"]["totalCount"],
        "langs": top,
        "loc_net": loc_net,
        "loc_add": loc_add,
        "loc_del": loc_del,
    }


def placeholder(login, sample=False):
    if sample:
        return {"login": login, "commits": 1342, "stars": 87, "prs": 214,
                "issues": 96, "repos": 38, "followers": 121,
                "langs": ["Python", "Go", "Shell", "TypeScript", "HCL"],
                "loc_net": 220608, "loc_add": 267558, "loc_del": 46950}
    return {"login": login, "commits": None, "stars": None, "prs": None,
            "issues": None, "repos": None, "followers": None, "langs": [],
            "loc_net": None, "loc_add": None, "loc_del": None}


def num(v):
    return "—" if v is None else f"{v:,}"


def line(x, y, delay, label, value, target):
    dots = max(1, target - (4 + len(label) + 2))
    return (
        f'    <text class="sln" style="animation-delay:{delay}s" x="{x}" y="{y}" xml:space="preserve">'
        f'<tspan fill="#BF00FF">  &#9656; </tspan>'
        f'<tspan fill="#E879F9">{escape(label)}</tspan>'
        f'<tspan fill="#8A6F9E"> {"."*dots} </tspan>'
        f'<tspan fill="#D8C9E8">{escape(value)}</tspan></text>'
    )


def render(s):
    langs = " · ".join(s["langs"]) if s["langs"] else "computing…"
    if s["loc_net"] is None:
        loc_val = '<tspan fill="#D8C9E8">—</tspan>'
    else:
        loc_val = (
            f'<tspan fill="#D8C9E8">{s["loc_net"]:,} </tspan>'
            f'<tspan fill="#8A6F9E">(</tspan>'
            f'<tspan fill="#34D399">{s["loc_add"]:,}++</tspan>'
            f'<tspan fill="#8A6F9E">, </tspan>'
            f'<tspan fill="#FB7185">{s["loc_del"]:,}--</tspan>'
            f'<tspan fill="#8A6F9E">)</tspan>'
        )
    loc = (
        '    <text class="sln" style="animation-delay:1.20s" x="34" y="238" xml:space="preserve">'
        '<tspan fill="#BF00FF">  &#9656; </tspan>'
        '<tspan fill="#E879F9">lines of code</tspan>'
        '<tspan fill="#8A6F9E"> ... </tspan>' + loc_val + '</text>'
    )
    rows = [
        line(34, 150, 0.75, "commits", num(s["commits"]), 22),
        line(420, 150, 0.80, "stars", num(s["stars"]), 22),
        line(34, 178, 0.90, "pull requests", num(s["prs"]), 22),
        line(420, 178, 0.95, "issues", num(s["issues"]), 22),
        line(34, 206, 1.05, "repositories", num(s["repos"]), 22),
        line(420, 206, 1.10, "followers", num(s["followers"]), 22),
        loc,
        line(34, 268, 1.30, "top langs", langs, 22),
    ]
    rows = "\n".join(rows)
    login = escape(s["login"])
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 300" width="820" height="300" fill="none" font-family="'Courier New', ui-monospace, monospace" role="img" aria-label="github stats for {login}">
  <defs>
    <linearGradient id="gbody" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#140A1E"/>
      <stop offset="1" stop-color="#0B0712"/>
    </linearGradient>
    <filter id="gtext" x="-30%" y="-30%" width="160%" height="160%">
      <feGaussianBlur stdDeviation="1.1" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <filter id="gborder" x="-20%" y="-40%" width="140%" height="180%">
      <feGaussianBlur stdDeviation="3.5" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <pattern id="gscan" width="3" height="3" patternUnits="userSpaceOnUse">
      <rect width="3" height="1" fill="#E879F9" opacity="0.045"/>
    </pattern>
    <clipPath id="gwin"><rect x="10" y="10" width="800" height="280" rx="14"/></clipPath>
    <style>
      text {{ font-size:17px }}
      @keyframes gblink {{ 0%,50% {{ opacity:1 }} 51%,100% {{ opacity:0 }} }}
      @keyframes gin    {{ from {{ opacity:0; transform:translateX(-8px) }} to {{ opacity:1; transform:translateX(0) }} }}
      @keyframes gpulse {{ 0%,100% {{ opacity:.5 }} 50% {{ opacity:1 }} }}
      @keyframes gdotpulse {{ 0%,100% {{ opacity:.55; r:5 }} 50% {{ opacity:1; r:6 }} }}
      @keyframes gscanmove {{ from {{ transform:translateY(0) }} to {{ transform:translateY(3px) }} }}
      .gcaret {{ animation:gblink 1.1s steps(1) infinite }}
      .sln    {{ opacity:0; animation:gin .5s ease-out forwards }}
      .gglow  {{ animation:gpulse 3s ease-in-out infinite }}
      .gdot   {{ animation:gdotpulse 2s ease-in-out infinite }}
      .gscan  {{ animation:gscanmove .5s linear infinite }}
    </style>
  </defs>

  <rect x="10" y="10" width="800" height="280" rx="14" fill="url(#gbody)"/>
  <g clip-path="url(#gwin)">
    <g class="gscan"><rect x="10" y="7" width="800" height="286" fill="url(#gscan)"/></g>
  </g>
  <rect x="10" y="10" width="800" height="280" rx="14" fill="none" stroke="#BF00FF" stroke-width="1.5" class="gglow" filter="url(#gborder)"/>

  <circle cx="38" cy="30" r="6" fill="#BF00FF" filter="url(#gtext)"/>
  <circle cx="60" cy="30" r="6" fill="#E879F9" filter="url(#gtext)"/>
  <circle cx="82" cy="30" r="6" fill="#8B00CC" filter="url(#gtext)"/>
  <text x="410" y="35" text-anchor="middle" font-size="14" fill="#8A6F9E" letter-spacing="1">youkyi@infra: ~</text>
  <line x1="10" y1="50" x2="810" y2="50" stroke="#BF00FF" stroke-opacity="0.35" class="gglow"/>

  <text class="sln" style="animation-delay:.1s" x="30" y="84" filter="url(#gtext)">
    <tspan fill="#BF00FF">youkyi@infra:~$</tspan><tspan fill="#F3E8FF"> gh stats --user {login} </tspan><tspan class="gcaret" fill="#BF00FF">&#9608;</tspan>
  </text>

  <g filter="url(#gtext)">
    <circle class="gdot" cx="34" cy="114" r="5.5" fill="#34D399"/>
    <text class="sln" style="animation-delay:.55s" x="48" y="119"><tspan fill="#F3E8FF">infra.github</tspan><tspan fill="#8A6F9E"> : {escape(s.get("summary", "public activity summary"))}</tspan></text>

{rows}
  </g>
</svg>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.environ.get("STATS_OUT", "assets/stats.svg"))
    ap.add_argument("--login", default=os.environ.get("GH_LOGIN", "YouKyi"))
    ap.add_argument("--sample", action="store_true", help="use representative numbers (preview)")
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and not args.sample:
        data = fetch(token, args.login)
    else:
        if not args.sample:
            print("warning: no token, writing placeholder card", file=sys.stderr)
        data = placeholder(args.login, sample=args.sample)

    data["summary"] = (
        "private + public activity summary"
        if (bool(os.environ.get("GH_TOKEN")) or args.sample)
        else "public activity summary"
    )
    svg = render(data)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"wrote {args.out} (commits={data['commits']} stars={data['stars']} langs={data['langs']})")


if __name__ == "__main__":
    main()
