"""Refuse identifiers and secrets in a tree (the release-pages step). Standard library only.

--identifiers (the pre-commit hook, scripts/check.sh and CI):
  - a GCP billing account pattern ([0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}) or any 12-digit
    number (a project number's shape) in deploy/, docs/ and src/; uv.lock is allowed (its
    sizes and hashes are not identifiers);
  - when deploy/gke/env.sh exists (locally; never in CI or an export), the exact values of
    its identifying variables (PROJECT_ID, PROJECT_NUMBER, BILLING_ACCOUNT, BUCKET_NAME,
    with ${NAME} references filled) in any file.
--full (scripts/export-public.sh, on the exported tree) adds:
  - secrets: Anthropic, Google API and GitHub token shapes, private keys;
  - real email addresses (not noreply, example or .invalid ones, npm scopes or versions);
  - home paths (/home/<user>);
  - files over 5 MB.

Prints the rule, the file and the line of each match, never the matched text. Exit 1 on any.
The files: those git tracks or would track in a repository (untracked, not ignored), or
every file under --root when it is not a repository (an export).
"""

import argparse
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

GUARDED_DIRS = ("deploy/", "docs/", "src/")
ALLOWED = frozenset({"uv.lock"})
ENV_FILE = Path("deploy/gke/env.sh")
ENV_KEYS = ("PROJECT_ID", "PROJECT_NUMBER", "BILLING_ACCOUNT", "BUCKET_NAME")
MAX_BYTES = 5 * 1024 * 1024
SKIP_PARTS = frozenset({".git", "node_modules", ".venv", "var", "__pycache__", ".mypy_cache"})

BILLING = re.compile(rb"(?<![0-9A-Za-z])[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}(?![0-9A-Za-z])")
TWELVE_DIGITS = re.compile(rb"(?<![0-9A-Za-z-])[0-9]{12}(?![0-9A-Za-z])")  # not a UUID group
SECRETS = {
    "an Anthropic key": re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "a Google API key": re.compile(rb"AIza[0-9A-Za-z_\-]{30,}"),
    "a GitHub token": re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    "a GitHub fine-grained token": re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    "a private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
HOME = re.compile(rb"/home/[a-z_][a-z0-9_-]*")
_ENV_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=["']?([^"'#]*?)["']?\s*(?:#.*)?$""")


def env_identifiers(path: Path) -> dict[str, bytes]:
    """The identifying values of an env file, ${NAME} references filled; empty ones out."""
    raw: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _ENV_LINE.match(line)
            if m and m.group(2).strip():
                raw[m.group(1)] = m.group(2).strip()

    def fill(v: str) -> str:
        return re.sub(r"\$\{([A-Z][A-Z0-9_]*)\}", lambda m: raw.get(m.group(1), ""), v)

    return {k: fill(raw[k]).encode() for k in ENV_KEYS if k in raw and fill(raw[k])}


def harmless_email(e: bytes) -> bool:
    s = e.decode(errors="replace").lower()
    return (
        "noreply" in s
        or s.endswith((".invalid", "example.com", "example.org", ".example", ".png", ".svg"))
        or bool(re.match(r"^[0-9.]+@|^[^@]*@[0-9]", s))
        or bool(re.search(r"@(?:types|vitejs|tanstack|testing-library|typescript-eslint|eslint|"
                          r"playwright|babel|rollup|esbuild|vitest)\b", s))
    )  # fmt: skip


def files(root: Path) -> Iterator[Path]:
    git = subprocess.run(  # noqa: S603 (fixed argv, read-only git)
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if git.returncode == 0 and (root / ".git").exists():
        for name in git.stdout.decode().split("\0"):
            if name and (root / name).is_file():
                yield Path(name)
        return
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not (set(rel.parts) & SKIP_PARTS):
            yield rel


def line_of(data: bytes, pos: int) -> int:
    return data.count(b"\n", 0, pos) + 1


def scan(root: Path, full: bool, env_file: Path | None = None) -> list[str]:
    found: list[str] = []
    identifiers = env_identifiers(env_file if env_file is not None else root / ENV_FILE)
    for rel in files(root):
        name = rel.as_posix()
        if name == ENV_FILE.as_posix():
            continue  # the values' own home, git-ignored
        path = root / rel
        if full and path.stat().st_size > MAX_BYTES:
            found.append(f"a file over 5 MB: {name}")
            continue
        data = path.read_bytes()
        if b"\0" in data[:8000]:
            continue
        rules: list[tuple[str, re.Pattern[bytes]]] = []
        if name.startswith(GUARDED_DIRS) and name not in ALLOWED:
            rules += [("a billing account pattern", BILLING), ("a 12-digit number", TWELVE_DIGITS)]
        if full:
            rules += [*SECRETS.items(), ("a home path", HOME)]
        for rule, rx in rules:
            for m in rx.finditer(data):
                found.append(f"{rule}: {name}:{line_of(data, m.start())}")
        if full:
            for m in EMAIL.finditer(data):
                if not harmless_email(m.group(0)):
                    found.append(f"a real email address: {name}:{line_of(data, m.start())}")
        for key, value in identifiers.items():
            start = data.find(value)
            while start != -1:
                found.append(f"the value of {key} in {ENV_FILE}: {name}:{line_of(data, start)}")
                start = data.find(value, start + 1)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--identifiers", action="store_true")
    mode.add_argument("--full", action="store_true")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="The env file whose values to refuse (default: <root>/deploy/gke/env.sh); an "
        "export has none of its own, so export-public.sh passes the private clone's.",
    )
    args = parser.parse_args(argv)
    if args.env_file is not None and not args.env_file.is_file():
        print(f"no env file at {args.env_file}", file=sys.stderr)
        return 2
    found = scan(args.root.resolve(), full=args.full, env_file=args.env_file)
    for line in found:
        print(f"refused: {line}", file=sys.stderr)
    what = "full public scan" if args.full else "identifier guard"
    print(f"{what}: {len(found)} match(es)" + ("" if found else ", clean"))
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
