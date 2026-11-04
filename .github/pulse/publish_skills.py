"""Publish this repo's marked skills to Pulse.

A skill is marked in its SKILL.md frontmatter, under `metadata:`:

    metadata:
      pulse: shareable          # or: internal
      pulse-summary: One line, 10 to 160 characters, saying what the skill is for.

Anything else (no `metadata.pulse`, another value) means "not published". On every merge to
main a GitHub Action runs this script, which sends Pulse each marked skill: the SKILL.md body
without its frontmatter, then each references/*.md beside it. Pulse is authoritative and checks
everything again; the checks here repeat its rules (server/agentPlatform/skillsPure.ts) so a run
fails before sending what Pulse would refuse. Shareable skills reach customers' agents: no long
dashes, no CRM vendor names. Every skill: no secrets.

The five skill repos carry identical copies of this file; odf-skills-meta/_template is the source.

Usage:
  python -I .github/pulse/publish_skills.py --dry-run   # list what would be sent, send nothing
  python -I .github/pulse/publish_skills.py             # needs SKILLS_PUBLISH_SECRET (and PULSE_URL)
"""
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://pulse.opendealflow.com"
ROUTE = "/api/webhooks/skills/publish"
SKIP_DIRS = {".git", "dist", "node_modules", ".worktrees", ".github"}
AUDIENCES = ("shareable", "internal")
SUMMARY_MIN, SUMMARY_MAX = 10, 160
SKILL_MAX_CHARS, PUBLISH_MAX_CHARS, SKILLS_PER_REPO = 400_000, 5_000_000, 50
PATH_MAX, VERSION_MAX = 300, 40
NAME_RE = re.compile(r"^odf-[a-z0-9][a-z0-9-]{0,59}$")
REPO_RE = re.compile(r"^JD-OD/odf-[a-z0-9-]+$")

# Customer-facing text rules, the same as customerTextIssue in skillsPure.ts.
VENDOR_RE = re.compile(r"(?<![A-Za-z])(go[\s_-]*high[\s_-]*level|highlevel|ghl|leadconnector)(?![A-Za-z])", re.I)
LONG_DASH_RE = re.compile("[\u2012\u2013\u2014\u2015\u2212]")
INVISIBLE_RE = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")
LINE_BREAK_RE = re.compile("[\r\n\u2028\u2029\u0085\u000b\u000c]")
LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")

# The secret check, the same as looksLikeSecret in brainPure.ts.
SECRET_RE = re.compile(r"sk-[A-Za-z0-9_-]{16,}|pak_[A-Za-z0-9_]{8,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|[A-Za-z0-9+/=_]{32,}")
PREFIXED_RE = re.compile(r"^(sk-|pak_|ghp_|github_pat_)")
URL_RE = re.compile(r"https?://\S+")
KEY_PIECE = 16

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
BLOCK_RE = re.compile(r"^[>|][+-]?\d*[ \t]*(#.*)?$")
VERSION_LINE_RE = re.compile(r"^\*\*Version:\s*v?([0-9][^\s*]*)\s*\*\*", re.M)

WARN_PATTERNS = (
    ("a dollar amount", re.compile(r"\$\d")),
    ("ROI", re.compile(r"\bROI\b", re.I)),
    ("markup", re.compile(r"\bmarkup\b", re.I)),
    ("hours", re.compile(r"\bhours?\b", re.I)),
)


class SkillError(Exception):
    """A marked skill that breaks the rules; the publish stops before sending anything."""


def u16len(s):
    """Length as JavaScript counts it (UTF-16 code units), which is what Pulse's limits use."""
    return len(s.encode("utf-16-le")) // 2


def _fold(line):
    return INVISIBLE_RE.sub("", unicodedata.normalize("NFKC", line))


def customer_text_issue(text):
    """The first customer-facing problem ('line 12 has a long dash'), or None. Same rules as Pulse."""
    for i, raw in enumerate(LINE_SPLIT_RE.split(text)):
        line = _fold(raw)
        if LONG_DASH_RE.search(raw) or LONG_DASH_RE.search(line):
            return f"line {i + 1} has a long dash"
        if VENDOR_RE.search(line):
            return f"line {i + 1} names the CRM vendor"
    return None


def _mixes(s):
    return bool(re.search(r"[A-Za-z]", s)) and bool(re.search(r"\d", s))


def looks_like_secret(text):
    if not text:
        return False
    for m in SECRET_RE.finditer(text):
        if PREFIXED_RE.match(m.group(0)):
            return True
    for m in SECRET_RE.finditer(URL_RE.sub(" ", text)):
        s = m.group(0)
        if PREFIXED_RE.match(s) or not _mixes(s):
            continue
        if "_" not in s:
            return True
        if any(len(p) >= KEY_PIECE and _mixes(p) for p in s.split("_")):
            return True
    return False


def _unquote(v):
    """A scalar value: quotes removed, or an unquoted ' # comment' tail removed."""
    v = v.strip()
    if v and v[0] in "\"'":
        end = v.find(v[0], 1)
        if end > 0:
            return v[1:end]
        return v
    return re.sub(r"(^|\s+)#.*$", "", v).strip()


def _indent(line):
    return len(line) - len(line.lstrip(" \t"))


def parse_frontmatter(text):
    """(top-level scalars, metadata children, body). BOM and CRLF tolerated; block scalars (> and |) skipped."""
    text = text.lstrip("\ufeff")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, {}, text
    top, meta = {}, {}
    in_meta = False
    skip_above = None  # skip lines indented deeper than this while inside a block scalar
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = _indent(line)
        if skip_above is not None:
            if ind > skip_above:
                continue
            skip_above = None
        if ind == 0:
            if ":" not in line:
                in_meta = False
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            in_meta = k.lower() == "metadata" and not v
            if BLOCK_RE.match(v):
                skip_above = 0
                continue
            if not in_meta:
                top[k] = _unquote(v)
        elif in_meta and ":" in line and not line.lstrip().startswith("-"):
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if BLOCK_RE.match(v):
                skip_above = ind
                continue
            meta[k] = _unquote(v)
    return top, meta, text[m.end():]


def _git_tracked(repo_root):
    """Set of tracked files (posix, relative), or None when this is not a git checkout with files."""
    try:
        r = subprocess.run(["git", "-C", str(repo_root), "ls-files", "-z"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return {p for p in r.stdout.decode("utf-8", "replace").split("\0") if p}


def _read_text(p):
    return p.read_bytes().decode("utf-8", "replace").lstrip("\ufeff").replace("\r\n", "\n")


def _config_version(path):
    if not path.is_file():
        return None
    m = re.search(r"^VERSION:\s*[\"']?([^\"'\s#]+)", _read_text(path), re.M)
    return m.group(1) if m else None


def skill_version(skill_md, repo_root, body):
    """S2-R5: the **Version: x.y.z** line, else config.yaml VERSION beside the skill or at the repo root,
    else the nearest .claude-plugin/plugin.json version walking up; else None."""
    m = VERSION_LINE_RE.search(body)
    if m:
        return m.group(1)
    skill_dir, repo_root = skill_md.resolve().parent, repo_root.resolve()
    for cfg in (skill_dir / "config.yaml", repo_root / "config.yaml"):
        v = _config_version(cfg)
        if v:
            return v
    d = skill_dir
    while True:
        pj = d / ".claude-plugin" / "plugin.json"
        if pj.is_file():
            try:
                v = json.loads(_read_text(pj)).get("version")
            except (ValueError, AttributeError):
                v = None
            if isinstance(v, str) and v:
                return v
        if d == repo_root or d.parent == d:
            return None
        d = d.parent


def _rel(path, repo_root):
    """Repo-relative posix path; a file (or symlink target) outside the repo is a SkillError."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        raise SkillError(f"{path.as_posix()}: resolves outside the repo (a symlink?); only files inside the repo are published")


def _references(skill_md, repo_root, tracked):
    refs = skill_md.parent / "references"
    if not refs.is_dir():
        return []
    out = []
    for p in sorted(refs.glob("*.md"), key=lambda x: x.name):
        if not p.is_file():
            continue
        rel = _rel(p, repo_root)
        if tracked is not None and rel not in tracked:
            continue
        out.append(p)
    return out


def build_skill(skill_md, repo_root, _tracked=None):
    """The skill as Pulse takes it, or None when unmarked. Raises SkillError for a marked skill that breaks the rules."""
    skill_md, repo_root = Path(skill_md), Path(repo_root)
    rel = _rel(skill_md, repo_root)
    top, meta, body = parse_frontmatter(_read_text(skill_md))

    def fail(msg):
        raise SkillError(f"{rel}: {msg}")

    if "pulse" not in meta:
        return None
    audience = meta["pulse"].strip().lower()
    if audience not in AUDIENCES:
        fail(f"metadata.pulse must be shareable or internal (got {meta['pulse']!r}); remove it to leave the skill unpublished")

    name = top.get("name", "").strip()
    if not NAME_RE.match(name):
        fail(f"name must match {NAME_RE.pattern} (got {name!r})")
    summary = meta.get("pulse-summary", "").strip()
    if not summary:
        fail("marked for Pulse but metadata.pulse-summary is missing")
    if not (SUMMARY_MIN <= u16len(summary) <= SUMMARY_MAX) or LINE_BREAK_RE.search(summary):
        fail(f"pulse-summary must be one line of {SUMMARY_MIN} to {SUMMARY_MAX} characters")
    if len(rel) > PATH_MAX:
        fail(f"path is longer than {PATH_MAX} characters")

    text = body.lstrip("\n")
    for ref in _references(skill_md, repo_root, _tracked):
        text = text.rstrip("\n") + f"\n\n## File: references/{ref.name}\n\n" + _read_text(ref).strip("\n") + "\n"
    text = text.strip("\n") + "\n" if text.strip() else ""
    if not text.strip():
        fail("the body is empty")
    if u16len(text) > SKILL_MAX_CHARS:
        fail(f"the text is {u16len(text)} characters; the limit is {SKILL_MAX_CHARS}")
    if "\u0000" in summary or "\u0000" in text:
        fail("contains a NUL character")
    if text.lstrip().startswith("---"):
        fail("the body starts with '---' (frontmatter or a rule); Pulse refuses it")
    if looks_like_secret(summary) or looks_like_secret(text):
        fail("the summary or text looks like it contains a secret or key")
    version = skill_version(skill_md, repo_root, body)
    if version is not None and u16len(version) > VERSION_MAX:
        fail(f"version is longer than {VERSION_MAX} characters")
    if audience == "shareable":
        issue = customer_text_issue(summary)
        if issue:
            fail(f"summary {issue}; shareable skills reach customers")
        issue = customer_text_issue(text)
        if issue:
            fail(f"{issue}; shareable skills reach customers")
    return {"name": name, "path": rel, "audience": audience, "summary": summary, "version": version, "body": text}


def _skill_files(repo_root):
    tracked = _git_tracked(repo_root)
    if tracked is not None:
        found = [repo_root / p for p in tracked if p.rsplit("/", 1)[-1] == "SKILL.md"
                 and not (set(p.split("/")[:-1]) & SKIP_DIRS)]
    else:
        found = []
        for dirpath, dirnames, filenames in os.walk(repo_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            if "SKILL.md" in filenames:
                found.append(Path(dirpath) / "SKILL.md")
    return sorted((p for p in found if p.is_file()), key=lambda p: p.as_posix()), tracked


def collect(repo_root):
    """Every marked skill in the repo; duplicate names, too many skills or too much text raise SkillError."""
    repo_root = Path(repo_root)
    files, tracked = _skill_files(repo_root)
    skills, seen = [], {}
    for f in files:
        s = build_skill(f, repo_root, tracked)
        if s is None:
            continue
        if s["name"] in seen:
            raise SkillError(f"{s['path']}: {s['name']} is also published from {seen[s['name']]}; rename one of them")
        seen[s["name"]] = s["path"]
        skills.append(s)
    if len(skills) > SKILLS_PER_REPO:
        raise SkillError(f"{len(skills)} skills; a repo may publish up to {SKILLS_PER_REPO}")
    if sum(u16len(s["body"]) for s in skills) > PUBLISH_MAX_CHARS:
        raise SkillError(f"a publish may be up to {PUBLISH_MAX_CHARS} characters in all")
    return skills


def content_warnings(skill):
    """Shareable skills only: lines that look like dollars, ROI, markup or hours claims. Printed, never a refusal."""
    if skill.get("audience") != "shareable":
        return []
    out = []
    for i, line in enumerate(LINE_SPLIT_RE.split(skill.get("body", ""))):
        for label, rx in WARN_PATTERNS:
            if rx.search(line):
                out.append(f"line {i + 1} mentions {label}: {line.strip()[:80]}")
    return out


def commit_sha(repo_root):
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha
    r = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    root = Path.cwd()
    try:
        skills = collect(root)
    except SkillError as e:
        print(f"[publish] {e}", file=sys.stderr)
        sys.exit(1)
    sha = commit_sha(root)
    if "--dry-run" in argv:
        for s in skills:
            print(f"[publish] {s['name']}  {s['audience']}  v{s['version'] or '-'}  ({len(s['body'])} chars)  {s['path']}")
            for w in content_warnings(s):
                print(f"[publish] warning {s['name']}: {w}")
        print(f"[publish] {len(skills)} marked skill(s) at {sha[:7] or 'unknown'}; nothing sent (dry run)")
        return 0
    secret = os.environ.get("SKILLS_PUBLISH_SECRET", "")
    if len(secret) < 32:
        print("[publish] SKILLS_PUBLISH_SECRET is missing or too short; nothing sent", file=sys.stderr)
        sys.exit(1)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not REPO_RE.match(repo):
        print(f"[publish] GITHUB_REPOSITORY must look like JD-OD/odf-<name> (got {repo!r}); nothing sent", file=sys.stderr)
        sys.exit(1)
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
        print("[publish] no commit sha (GITHUB_SHA or git HEAD); nothing sent", file=sys.stderr)
        sys.exit(1)
    url = os.environ.get("PULSE_URL", DEFAULT_URL).rstrip("/") + ROUTE
    # ensure_ascii=False: \uXXXX escapes would make a legal publish bigger on the wire (20 MB body limit).
    data = json.dumps({"repo": repo, "commitSha": sha, "skills": skills}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8", "x-skills-secret": secret})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(f"[publish] {r.status}: {r.read().decode('utf-8', 'replace')}")
    except urllib.error.HTTPError as e:
        print(f"[publish] Pulse refused the publish ({e.code}): {e.read().decode('utf-8', 'replace')}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[publish] could not reach Pulse at {url}: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"[publish] could not reach Pulse at {url}: {e}", file=sys.stderr)
        sys.exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
