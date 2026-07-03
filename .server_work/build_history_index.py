#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class IndexedFile:
    category: str
    relative_path: str
    filename: str


def collect_files(root: Path) -> list[IndexedFile]:
    mapping: list[tuple[str, str]] = [
        ("logs", "logs"),
        ("journal_reviews", "journal/*_ai_review.md"),
        ("journal_changes", "journal/*_ai_changes.json"),
        ("journal_strategy", "journal/strategy_*"),
        ("journal_active_profile", "journal/active_strategy_profile.json"),
        ("account_migrations", "data/account_migrations/*"),
        ("runtime_state", "data/paper_state.json"),
        ("runtime_state", "data/realtime_runtime.json"),
        ("runtime_state", "data/kiwoom_mock_account_state.json"),
        ("runtime_state", "data/kiwoom_token_cache.json"),
        ("review", "review/*"),
        ("patterns", "data/patterns/*"),
        ("backtest_cache", "data/bt_cache/*"),
    ]
    items: list[IndexedFile] = []
    seen: set[str] = set()
    for category, pattern in mapping:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            rel = str(path.relative_to(root))
            if rel in seen:
                continue
            seen.add(rel)
            items.append(IndexedFile(category=category, relative_path=rel, filename=path.name))
    return items


def build_markdown(root: Path, items: list[IndexedFile]) -> str:
    lines = [
        "# History Index",
        "",
        f"Root: `{root}`",
        "",
        "## Indexed Files",
        "",
    ]
    grouped: dict[str, list[IndexedFile]] = {}
    for item in items:
        grouped.setdefault(item.category, []).append(item)
    for category in sorted(grouped):
        lines.append(f"### {category}")
        for item in grouped[category]:
            lines.append(f"- `{item.relative_path}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build searchable history indexes for a bot root")
    parser.add_argument("--root", required=True, help="Bot root path to index")
    parser.add_argument("--outdir", required=True, help="Directory where index files are written")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    items = collect_files(root)
    (outdir / "history-index.json").write_text(
        json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (outdir / "history-index.md").write_text(build_markdown(root, items), encoding="utf-8")
    print(f"indexed {len(items)} files -> {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
