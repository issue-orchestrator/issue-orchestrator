#!/usr/bin/env python3
"""Render local Markdown through GitHub's renderer and preview it locally.

This gives a closer README preview than a generic Markdown parser:

- GitHub's Markdown API handles GFM details and GitHub-specific enrichment.
- Local relative links and images resolve from the repository root.
- Mermaid enrichment blocks are rendered client-side so diagrams are visible.
"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / ".preview" / "README.html"


def _run_text(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=True,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def _detect_repo_context() -> str | None:
    explicit = os.environ.get("GITHUB_REPOSITORY")
    if explicit:
        return explicit

    remote = _run_text(["git", "config", "--get", "remote.origin.url"])
    if not remote:
        return None

    patterns = (
        r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.match(pattern, remote)
        if match:
            return match.group("repo")
    return None


def _github_token() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name)
        if token:
            return token
    return _run_text(["gh", "auth", "token"])


def _render_with_github(markdown: str, repo_context: str | None) -> str:
    payload: dict[str, str] = {"text": markdown, "mode": "gfm"}
    if repo_context:
        payload["context"] = repo_context

    headers = {
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "issue-orchestrator-markdown-preview",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        "https://api.github.com/markdown",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        message = f"GitHub Markdown API failed: HTTP {error.code}"
        if detail:
            message = f"{message}\n{detail}"
        raise SystemExit(message) from error
    except urllib.error.URLError as error:
        raise SystemExit(f"GitHub Markdown API failed: {error}") from error


def _repo_base_uri() -> str:
    uri = REPO_ROOT.resolve().as_uri()
    return uri if uri.endswith("/") else f"{uri}/"


def _build_preview_document(rendered_html: str, source_path: Path) -> str:
    title = html.escape(source_path.name)
    base_uri = html.escape(_repo_base_uri(), quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <base href="{base_uri}">
  <title>{title} preview</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css/github-markdown-light.min.css">
  <style>
    body {{
      margin: 0;
      background: #f6f8fa;
      color: #1f2328;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
    }}
    .preview-shell {{
      box-sizing: border-box;
      max-width: 1012px;
      margin: 32px auto;
      padding: 0 16px;
    }}
    .markdown-body {{
      box-sizing: border-box;
      min-width: 200px;
      max-width: 980px;
      padding: 45px;
      background: #fff;
      border: 1px solid #d0d7de;
      border-radius: 6px;
    }}
    .markdown-body img {{
      max-width: 100%;
    }}
    .github-mermaid {{
      display: flex;
      justify-content: center;
      overflow-x: auto;
      margin: 16px 0;
    }}
    .github-mermaid svg {{
      max-width: 100%;
      height: auto;
    }}
    .mermaid-error {{
      color: #cf222e;
      white-space: pre-wrap;
    }}
    @media (max-width: 767px) {{
      .preview-shell {{
        margin: 0;
        padding: 0;
      }}
      .markdown-body {{
        padding: 24px;
        border: 0;
        border-radius: 0;
      }}
    }}
  </style>
</head>
<body>
  <main class="preview-shell">
    <article class="markdown-body">
{rendered_html}
    </article>
  </main>
  <script type="module">
    import mermaid from "https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.esm.min.mjs";

    mermaid.initialize({{ startOnLoad: false, securityLevel: "strict", theme: "default" }});

    function mermaidSourceFrom(section) {{
      const target = section.querySelector(".js-render-enrichment-target");
      return target?.getAttribute("data-plain")
        || target?.querySelector('pre[lang="mermaid"]')?.textContent
        || "";
    }}

    async function renderMermaidBlock(code, id) {{
      const container = document.createElement("div");
      container.className = "github-mermaid";
      try {{
        const result = await mermaid.render(id, code);
        container.innerHTML = result.svg;
        result.bindFunctions?.(container);
      }} catch (error) {{
        container.className = "mermaid-error";
        container.textContent = `${{error}}\\n\\n${{code}}`;
      }}
      return container;
    }}

    async function renderGithubEnrichmentBlocks() {{
      const sections = Array.from(document.querySelectorAll('section[data-type="mermaid"]'));
      for (let index = 0; index < sections.length; index += 1) {{
        const code = mermaidSourceFrom(sections[index]);
        if (!code.trim()) {{
          continue;
        }}
        const rendered = await renderMermaidBlock(code, `github-mermaid-${{index}}`);
        sections[index].replaceChildren(rendered);
      }}
    }}

    async function renderPlainMermaidFallbacks() {{
      const blocks = Array.from(document.querySelectorAll('.highlight-source-mermaid, pre[lang="mermaid"]'));
      for (let index = 0; index < blocks.length; index += 1) {{
        const block = blocks[index];
        if (block.closest('section[data-type="mermaid"]')) {{
          continue;
        }}
        const code = block.textContent || "";
        if (!code.trim()) {{
          continue;
        }}
        const rendered = await renderMermaidBlock(code, `plain-mermaid-${{index}}`);
        block.replaceWith(rendered);
      }}
    }}

    await renderGithubEnrichmentBlocks();
    await renderPlainMermaidFallbacks();
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="README.md", help="Markdown file to preview")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="HTML output path, default: .preview/README.html",
    )
    parser.add_argument(
        "--repo-context",
        default=None,
        help="GitHub repo context, for example BruceBGordon/issue-orchestrator",
    )
    parser.add_argument("--open", action="store_true", help="Open the generated preview in a browser")
    args = parser.parse_args()

    source_path = (REPO_ROOT / args.path).resolve()
    if not source_path.exists():
        raise SystemExit(f"Markdown file not found: {source_path}")
    if not source_path.is_relative_to(REPO_ROOT):
        raise SystemExit(f"Markdown file must be inside the repo: {source_path}")

    repo_context = args.repo_context or _detect_repo_context()
    markdown = source_path.read_text(encoding="utf-8")
    rendered_html = _render_with_github(markdown, repo_context)

    output_path = (REPO_ROOT / args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        _build_preview_document(rendered_html, source_path),
        encoding="utf-8",
    )

    print(f"Wrote {output_path}")
    print("Markdown rendered by GitHub API; Mermaid rendered locally in the browser.")
    if repo_context:
        print(f"Repository context: {repo_context}")
    if args.open:
        webbrowser.open(output_path.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
