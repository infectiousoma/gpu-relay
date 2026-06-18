"""
GPU-Relay Workspace Tools — Open WebUI Tool Definition
=======================================================
Load this in Open WebUI → Admin Panel → Tools → Add Tool.
Set WORKSPACE_TOOLS_URL to the workspace-tools service (default: http://workspace-tools:7000).

Gives any model in Open WebUI the ability to:
  • Read / write / delete / move files in the shared workspace
  • Create directories and browse the file tree
  • Search file contents by regex
  • Execute Python or Bash code in the workspace
  • Generate a PDF from Markdown content
"""

import json
import os
import requests
from pydantic import BaseModel, Field

TOOLS_URL = os.environ.get("WORKSPACE_TOOLS_URL", "http://workspace-tools:7000")


class Tools:
    class Valves(BaseModel):
        workspace_tools_url: str = Field(
            default=TOOLS_URL,
            description="URL of the workspace-tools service",
        )

    def __init__(self):
        self.valves = self.Valves()

    def _url(self, path: str) -> str:
        return f"{self.valves.workspace_tools_url.rstrip('/')}/{path.lstrip('/')}"

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(self._url(path), json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    # ── File ops ──────────────────────────────────────────────────────────

    def write_file(self, path: str, content: str) -> str:
        """Write text content to a file in the workspace. Creates parent directories automatically.

        :param path: File path relative to workspace root (e.g. "my-project/src/main.py")
        :param content: Full text content to write
        """
        result = self._post("files/write", {"path": path, "content": content})
        return f"Written: {result['written']} ({result['bytes']} bytes)"

    def read_file(self, path: str) -> str:
        """Read a file from the workspace.

        :param path: File path relative to workspace root
        """
        r = requests.get(self._url("files/read"), params={"path": path}, timeout=30)
        r.raise_for_status()
        data = r.json()
        return f"=== {data['path']} ===\n{data['content']}"

    def delete_path(self, path: str) -> str:
        """Delete a file or directory from the workspace.

        :param path: File or directory path relative to workspace root
        """
        r = requests.delete(self._url("files/delete"), params={"path": path}, timeout=30)
        r.raise_for_status()
        return f"Deleted: {r.json()['deleted']}"

    def create_directory(self, path: str) -> str:
        """Create a directory (and any missing parents) in the workspace.

        :param path: Directory path relative to workspace root
        """
        result = self._post("files/mkdir", {"path": path})
        return f"Created directory: {result['created']}"

    def move_path(self, src: str, dst: str) -> str:
        """Move or rename a file or directory within the workspace.

        :param src: Source path relative to workspace root
        :param dst: Destination path relative to workspace root
        """
        result = self._post("files/move", {"src": src, "dst": dst})
        m = result["moved"]
        return f"Moved: {m['from']} → {m['to']}"

    def list_tree(self, path: str = ".", depth: int = 4) -> str:
        """List the directory tree of the workspace (or a subdirectory).

        :param path: Directory to list (default: workspace root)
        :param depth: How many levels deep to recurse (default: 4)
        """
        r = requests.get(self._url("files/tree"), params={"path": path, "depth": depth}, timeout=30)
        r.raise_for_status()
        return r.json()["tree"]

    def search_files(self, query: str, path: str = ".", glob: str = "*") -> str:
        """Search file contents in the workspace using a regex pattern.

        :param query: Regex pattern to search for
        :param path: Directory to search in (default: workspace root)
        :param glob: Glob pattern to filter files (e.g. "*.py", "*.md")
        """
        result = self._post("files/search", {"query": query, "path": path, "glob": glob})
        if not result["results"]:
            return f"No matches for '{query}'"
        lines = [f"Found in {result['total_files']} file(s):"]
        for item in result["results"]:
            lines.append(f"\n{item['file']}:")
            for m in item["matches"]:
                lines.append(f"  L{m['line']}: {m['text']}")
        return "\n".join(lines)

    # ── Code execution ────────────────────────────────────────────────────

    def run_python(self, code: str) -> str:
        """Execute Python code in the workspace directory. Output is captured and returned.

        :param code: Python code to execute
        """
        result = self._post("execute", {"code": code, "language": "python"})
        out = result["stdout"]
        err = result["stderr"]
        rc = result["returncode"]
        parts = [f"Exit: {rc}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        return "\n".join(parts)

    def run_bash(self, command: str) -> str:
        """Execute a bash command in the workspace directory.

        :param command: Shell command to run (e.g. "npm install", "git init")
        """
        result = self._post("execute", {"code": command, "language": "bash"})
        out = result["stdout"]
        err = result["stderr"]
        rc = result["returncode"]
        parts = [f"Exit: {rc}"]
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        return "\n".join(parts)

    # ── PDF generation ────────────────────────────────────────────────────

    def generate_pdf(self, markdown_content: str, output_path: str) -> str:
        """Render Markdown content to a PDF file saved in the workspace.

        :param markdown_content: Full Markdown text to render
        :param output_path: Output file path relative to workspace root (e.g. "docs/report.pdf")
        """
        result = self._post("generate/pdf", {
            "markdown": markdown_content,
            "output_path": output_path,
        })
        return f"PDF written: {result['pdf']} ({result['bytes']:,} bytes)"
