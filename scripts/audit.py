#!/usr/bin/env python3
import io
import json
import os
import re
import tarfile
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

UPSTREAM = os.environ["UPSTREAM"]
OLD = os.environ["OLD"]
NEW = os.environ["NEW"]
TOKEN = os.environ.get("GH_TOKEN", "")
OUT = Path(os.environ.get("OUT", "results"))
PROMPT_LIMIT = int(os.environ.get("PROMPT_LIMIT", "100000"))

SKIP_PATH = re.compile(r"(_test\.go$|(^|/)testdata/|^docs/|^examples/|\.md$|^\.github/ISSUE_TEMPLATE/)")
RISKY_PATH = re.compile(r"(^go\.mod$|^\.github/workflows/|^\.goreleaser|(^|/)Makefile$|(^|/)Dockerfile|\.sh$|update)")
PATTERNS = {
    "process execution": re.compile(r"exec\.Command|\"os/exec\"|syscall\.Exec"),
    "network call": re.compile(r"http\.(Get|Post|Head|NewRequest)|net\.Dial|\.Do\(req"),
    "long encoded blob": re.compile(r"[A-Za-z0-9+/=_-]{100,}"),
    "cgo or unsafe": re.compile(r"import \"C\"|\bunsafe\."),
    "download tool": re.compile(r"\b(curl|wget)\b"),
}
URL_RE = re.compile(r"https?://[^\s\"'`)<>\]]+")

findings = []
details = []


def api(path, raw=False):
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN and urlparse(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {TOKEN}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=180) as r:
        data = r.read()
    return data if raw else json.loads(data)


def raw_file(ref, path):
    return api(f"https://raw.githubusercontent.com/{UPSTREAM}/{ref}/{path}", raw=True).decode()


def add(status, text):
    findings.append((status, text))


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def check_release():
    new = api(f"/repos/{UPSTREAM}/releases/tags/v{NEW}")
    old = api(f"/repos/{UPSTREAM}/releases/tags/v{OLD}")
    na, oa = new["author"]["login"], old["author"]["login"]
    add("OK" if na == oa else "WARN", f"Release author `{na}` (v{OLD}: `{oa}`)")
    if new["prerelease"] or new["draft"]:
        add("WARN", "Release is marked prerelease or draft")
    nu = sorted({a["uploader"]["login"] for a in new["assets"]})
    ou = sorted({a["uploader"]["login"] for a in old["assets"]})
    add("OK" if nu == ou else "WARN", f"Asset uploaders {nu} (v{OLD}: {ou})")
    published = ts(new["published_at"])
    late = [a["name"] for a in new["assets"] if ts(a["updated_at"]) > published + timedelta(hours=1)]
    add("WARN" if late else "OK",
        f"Assets changed more than 1h after publishing: {late}" if late
        else f"No asset changed more than 1h after publishing ({new['published_at']})")
    return new


def tag_info(version):
    ref = api(f"/repos/{UPSTREAM}/git/ref/tags/v{version}")
    obj = ref["object"]
    tag_verified = None
    tagger = None
    if obj["type"] == "tag":
        tag = api(obj["url"])
        tag_verified = tag["verification"]["verified"]
        tagger = f"{tag['tagger']['name']} <{tag['tagger']['email']}>"
        obj = tag["object"]
    commit = api(f"/repos/{UPSTREAM}/commits/{obj['sha']}")
    return {
        "sha": obj["sha"],
        "tagger": tagger,
        "tag_verified": tag_verified,
        "commit_verified": commit["commit"]["verification"]["verified"],
        "commit_author": (commit.get("author") or {}).get("login") or commit["commit"]["author"]["name"],
    }


def check_tag():
    new, old = tag_info(NEW), tag_info(OLD)
    for key, label in (("tag_verified", "Tag signature verified"), ("commit_verified", "Commit signature verified")):
        bad = new[key] is False and old[key] is True
        add("WARN" if bad else ("OK" if new[key] else "INFO"), f"{label}: {new[key]} (v{OLD}: {old[key]})")
    add("INFO", f"Tagger {new['tagger']}, commit `{new['sha'][:12]}` by `{new['commit_author']}`")
    default = api(f"/repos/{UPSTREAM}")["default_branch"]
    status = api(f"/repos/{UPSTREAM}/compare/{new['sha']}...{default}")["status"]
    add("OK" if status in ("identical", "ahead") else "WARN",
        f"Tagged commit is on `{default}` (compare status: {status})")


def tar_listing(version):
    url = f"https://github.com/{UPSTREAM}/releases/download/v{version}/resticprofile_{version}_linux_amd64.tar.gz"
    data = api(url, raw=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        return sorted(f"{m.name} {oct(m.mode)}" for m in t.getmembers())


def check_tarball():
    new, old = tar_listing(NEW), tar_listing(OLD)
    add("OK" if new == old else "WARN", f"amd64 tarball contents: {new}" + ("" if new == old else f" (v{OLD}: {old})"))


def go_modules(ref):
    mods = set()
    block = False
    for line in raw_file(ref, "go.mod").splitlines():
        line = line.strip()
        if line.startswith("require ("):
            block = True
            continue
        if block and line == ")":
            block = False
            continue
        if block or line.startswith("require "):
            parts = line.removeprefix("require ").split()
            if parts and not parts[0].startswith("//"):
                mods.add(parts[0])
    return mods


def module_host(mod):
    return mod.split("/")[0]


def check_go_mod():
    new, old = go_modules(f"v{NEW}"), go_modules(f"v{OLD}")
    added = sorted(new - old)
    new_hosts = sorted({module_host(m) for m in new} - {module_host(m) for m in old})
    add("WARN" if new_hosts else "OK", f"New Go module hosts: {new_hosts or 'none'}")
    add("INFO", f"Added Go modules: {added or 'none'}; removed: {sorted(old - new) or 'none'}")


def scan_diff():
    cmp = api(f"/repos/{UPSTREAM}/compare/v{OLD}...v{NEW}")
    files = cmp["files"]
    authors = sorted({(c.get("author") or {}).get("login") or c["commit"]["author"]["name"] for c in cmp["commits"]})
    add("INFO", f"{cmp['total_commits']} commits, {len(files)} files changed, commit authors: {authors}")
    if len(files) >= 300:
        add("WARN", "Compare API returned 300 files; the diff is truncated")

    nopatch = [f["filename"] for f in files if "patch" not in f and f["status"] != "removed"]
    if nopatch:
        add("WARN", f"Files without a text patch (binary or too large): {nopatch}")

    hits = []
    hosts = {}
    for f in files:
        name = f["filename"]
        if SKIP_PATH.search(name) or name == "go.sum" or "patch" not in f:
            continue
        for line in f["patch"].splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            for label, rx in PATTERNS.items():
                if rx.search(line):
                    hits.append(f"- {label}: `{name}`: `{line[1:].strip()[:160]}`")
            for url in URL_RE.findall(line):
                host = urlparse(url).hostname or url
                hosts.setdefault(host, set()).add(name)
    add("WARN" if hits else "OK", f"Pattern hits in added non-test code: {len(hits)}")
    add("INFO", f"URL hosts in added non-test code: {sorted(hosts) or 'none'}")
    if hits:
        details.append("### Pattern hits\n\n" + "\n".join(hits[:200]))
    if hosts:
        details.append("### URL hosts\n\n" + "\n".join(f"- `{h}`: {sorted(v)}" for h, v in sorted(hosts.items())))
    return cmp


def build_prompt(cmp, release):
    files = [f for f in cmp["files"] if "patch" in f and f["filename"] != "go.sum"]
    files.sort(key=lambda f: (
        not RISKY_PATH.search(f["filename"]),
        bool(SKIP_PATH.search(f["filename"])),
        len(f["patch"]),
    ))
    head = [
        f"Upstream: https://github.com/{UPSTREAM}",
        f"Packaged artifacts: prebuilt linux tarballs, update from v{OLD} to v{NEW}.",
        "",
        "## Release notes (untrusted)",
        (release.get("body") or "")[:8000],
        "",
        "## Scripted checks",
        *[f"- {s}: {t}" for s, t in findings],
        "",
        "## Diff (untrusted, ordered by risk, may be truncated)",
    ]
    text = "\n".join(head)
    included, omitted = [], []
    for f in files:
        chunk = f"\n--- {f['filename']} ({f['status']}, +{f['additions']} -{f['deletions']})\n{f['patch']}\n"
        if len(text) + len(chunk) + 2000 < PROMPT_LIMIT:
            text += chunk
            included.append(f["filename"])
        else:
            omitted.append(f["filename"])
    if omitted:
        text += "\n## Files omitted for size\n" + "\n".join(omitted)
    add("INFO", f"AI prompt: {len(included)} files included, {len(omitted)} omitted, {len(text)} chars")
    (OUT / "prompt.txt").write_text(text)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    release = check_release()
    for fn in (check_tag, check_tarball, check_go_mod):
        try:
            fn()
        except Exception as e:
            add("WARN", f"{fn.__name__} failed: {e}")
    cmp = scan_diff()
    build_prompt(cmp, release)

    warns = sum(1 for s, _ in findings if s == "WARN")
    lines = [
        f"## Scripted audit (v{OLD} to v{NEW}): {warns} warning(s)",
        "",
        "| Result | Check |",
        "|---|---|",
        *[f"| {s} | {t.replace('|', '/')} |" for s, t in findings],
    ]
    if details:
        lines += ["", "<details><summary>Details</summary>", "", "\n\n".join(details), "", "</details>"]
    (OUT / "audit.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
