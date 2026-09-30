#!/usr/bin/env python3
"""Generate self-contained youkyi GitHub stats cards (desktop and mobile).

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
import base64
import datetime
import json
import os
import sys
import time
import textwrap
from pathlib import Path
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
    return "—" if v is None else f"{v:,}".replace(",", " ")


TEXT = {
    "fr": {
        "activity": "Activité GitHub", "unavailable": "Non disponibles",
        "private + public activity summary": "Contributions publiques + privées",
        "public activity summary": "Contributions publiques",
        "sample": "Exemple · données fictives", "summary": "Contributions GitHub",
        "metrics": ("Commits", "Étoiles", "Pull requests", "Issues", "Dépôts publics", "Abonnés"),
        "lines": "Lignes · solde des ajouts / suppressions",
        "lines_desc": "Lignes", "languages": "Langages",
    },
    "en": {
        "activity": "GitHub activity", "unavailable": "Unavailable",
        "private + public activity summary": "Public + private contributions",
        "public activity summary": "Public contributions",
        "sample": "Example · fictional data", "summary": "GitHub contributions",
        "metrics": ("Commits", "Stars", "Pull requests", "Issues", "Public repositories", "Followers"),
        "lines": "Lines · net additions / deletions",
        "lines_desc": "Lines", "languages": "Languages",
    },
}


def render(s, mobile=False, language="fr"):
    text = TEXT[language]
    # Embedded fonts keep SVG <img> rendering independent of external requests.
    fonts = Path(__file__).resolve().parents[2] / "assets/fonts"
    faces = "\n".join(
        f"@font-face{{font-family:'{family}';src:url(data:font/woff2;base64,"
        f"{base64.b64encode((fonts / filename).read_bytes()).decode()}) format('woff2')}}"
        for family, filename in [("Inter", "inter-400.woff2"),
                                 ("IBM Plex Mono", "ibm-plex-mono-400.woff2")]
    )
    width = 420 if mobile else 820
    langs = " · ".join(s["langs"]) or text["unavailable"]
    lang_rows = textwrap.wrap(langs, width=33 if mobile else 76)
    height = (430 if mobile else 318) + 24 * len(lang_rows)
    summary = text.get(s.get("summary"), text["summary"])
    metrics = list(zip(text["metrics"], ("commits", "stars", "prs", "issues", "repos", "followers")))
    rows = []
    for i, (label, key) in enumerate(metrics):
        x = 28 if mobile or i % 2 == 0 else 434
        y = 110 + 42 * (i if mobile else i // 2)
        right = width - 28 if mobile or i % 2 else 386
        rows.append(
            f'<text x="{x}" y="{y}" class="label">{escape(label)}</text>'
            f'<text x="{right}" y="{y}" text-anchor="end" class="value">{num(s[key])}</text>'
        )
    loc_y = 376 if mobile else 264
    loc = num(s["loc_net"])
    delta = f'+{num(s["loc_add"])} / −{num(s["loc_del"])}'
    rows.append(f'<text x="28" y="{loc_y - 28}" class="label">{escape(text["lines"])}</text>')
    rows.append(f'<text x="28" y="{loc_y}" class="value">{loc}</text>')
    rows.append(f'<text x="{width - 28}" y="{loc_y}" text-anchor="end" class="label">{delta}</text>')
    for i, row in enumerate(lang_rows):
        rows.append(f'<text x="28" y="{loc_y + 46 + 24 * i}" class="label">{escape(row)}</text>')
    login = escape(s["login"])
    description = escape(f"{summary}. " + "; ".join(
        f"{label} : {num(s[key])}" for label, key in metrics
    ) + f". {text['lines_desc']} : {loc}, {delta}. {text['languages']} : {langs}.")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" xml:lang="{language}" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-labelledby="title desc">
  <title id="title">{text['activity']} · {login}</title>
  <desc id="desc">{description}</desc>
  <style>
    {faces}
    .page {{ fill:#F8F7F4 }}
    .heading,.value {{ fill:#1A1816 }}
    .label {{ fill:#57534C }}
    .heading,.label {{ font-family:'Inter',sans-serif; font-weight:400 }}
    .heading {{ font-size:24px }}
    .label {{ font-size:18px }}
    .value {{ font-family:'IBM Plex Mono',monospace; font-size:22px }}
    @media (prefers-color-scheme:dark) {{
      .page {{ fill:#191715 }}
      .heading,.value {{ fill:#F2F0EC }}
      .label {{ fill:#B5AFA6 }}
    }}
  </style>
  <rect width="{width}" height="{height}" rx="16" class="page" />
  <text x="28" y="40" class="heading">{text['activity']} · {login}</text>
  <text x="28" y="68" class="label">{escape(summary)}</text>
  {"".join(rows)}
</svg>
'''


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
        "sample" if args.sample else "private + public activity summary"
        if bool(os.environ.get("GH_TOKEN"))
        else "public activity summary"
    )
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    for language in TEXT:
        localized = output.with_stem(output.stem + ("" if language == "fr" else f"-{language}"))
        for mobile in (False, True):
            target = localized.with_stem(localized.stem + ("-mobile" if mobile else ""))
            target.write_text(render(data, mobile=mobile, language=language), encoding="utf-8")
            print(f"wrote {target}")


if __name__ == "__main__":
    main()
