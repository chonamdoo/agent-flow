"""Pack measurement raw data for maintainer/improvement-history/ so a cycle can be re-read later.

    python tools/autoresearch/archive_runs.py --output maintainer/improvement-history/v<version>-raw.tar.gz \
        <name>=<phase_eval output dir> [...] [--file <name>=<path> ...]

Each run keeps results.jsonl and meta.json; the per-call evidence (full prompts and streams) is too large
and is reduced to evidence-stats.json, the counts cycle_report.py reads. Every text file is sanitised:
machine paths become placeholders and anything shaped like a credential or an email is redacted.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import tarfile
from pathlib import Path

from cycle_report import evidence_stats

# Order matters: specific prefixes before the generic /tmp rule.
PATHS = [
    (re.compile(r"/tmp/af-[\w.-]+"), "<work>"),
    (re.compile(r"/tmp/claude-\d+/[^\s\"'\\]*?/scratchpad"), "<scratch>"),
    (re.compile(r"/home/[\w.-]+/agent-flow"), "<repo>"),
    (re.compile(r"/home/[\w.-]+"), "<home>"),
    (re.compile(r"/root\b"), "<home>"),
    (re.compile(r"/tmp/[\w.-]+"), "<tmp>"),
    # Anything else that is a machine path (tool installs, PATH entries) says nothing the record needs.
    (re.compile(r"(?<![\w.<>/])/(?:opt|nix|usr|var|srv|mnt|etc|run|snap|Users|private|Library)(?:/[^\s\"':,;\\]*)?"), "<abs>"),
]
SECRETS = [
    re.compile(r"sk-ant-[\w-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_\w{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[\w.~+/-]{16,}=*"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)[^\s\"',]{8,}"),
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
]


def sanitise(text: str) -> str:
    for pattern, placeholder in PATHS:
        text = pattern.sub(placeholder, text)
    for pattern in SECRETS:
        text = pattern.sub(lambda m: m.group(1) + m.group(2) + "<redacted>" if m.re.groups == 2 else "<redacted>",
                           text)
    return text


def _add(archive: tarfile.TarFile, name: str, text: str) -> None:
    data = sanitise(text).encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(data), 0, 0o644
    archive.addfile(info, io.BytesIO(data))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("runs", nargs="*", help="name=<phase_eval output dir>")
    parser.add_argument("--file", action="append", default=[], help="name=<extra text file>, e.g. a loop results.tsv")
    args = parser.parse_args(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w:gz") as archive:
        for spec in args.runs:
            name, path = spec.split("=", 1)
            run = Path(path)
            for member in ("results.jsonl", "meta.json"):
                if (run / member).is_file():
                    _add(archive, f"{name}/{member}", (run / member).read_text(encoding="utf-8"))
            if (run / "evidence").is_dir():
                stats = evidence_stats([run / "evidence"])
                _add(archive, f"{name}/evidence-stats.json", json.dumps(
                    [{"stack": s, "kit": k, "trial": t, **values} for (s, k, t), values in sorted(stats.items())],
                    indent=1))
        for spec in args.file:
            name, path = spec.split("=", 1)
            _add(archive, name, Path(path).read_text(encoding="utf-8"))
    print(f"wrote {args.output} ({args.output.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
