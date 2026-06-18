"""Workspace Tools Service — file ops, search, code exec, PDF generation.

Runs as a sidecar to Open WebUI. Exposes REST endpoints that Open WebUI's
Python Tool shim calls. All paths are jail-rooted to WORKSPACE_ROOT.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
MAX_EXEC_TIMEOUT = int(os.environ.get("MAX_EXEC_TIMEOUT", "30"))
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE_MB", "10")) * 1024 * 1024


def _resolve(path: str) -> Path:
    """Resolve path relative to workspace root and reject traversal."""
    resolved = (WORKSPACE_ROOT / path.lstrip("/")).resolve()
    if not str(resolved).startswith(str(WORKSPACE_ROOT.resolve())):
        raise HTTPException(status_code=400, detail="Path outside workspace")
    return resolved


app = FastAPI(title="Workspace Tools", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Models ─────────────────────────────────────────────────────────────────

class WriteRequest(BaseModel):
    path: str
    content: str
    encoding: str = "utf-8"

class ReadRequest(BaseModel):
    path: str

class DeleteRequest(BaseModel):
    path: str

class MkdirRequest(BaseModel):
    path: str

class MoveRequest(BaseModel):
    src: str
    dst: str

class SearchRequest(BaseModel):
    query: str
    path: str = "."
    glob: str = "*"
    max_results: int = 50

class ExecRequest(BaseModel):
    code: str
    language: str = "python"  # python | bash
    timeout: int = 30

class PdfRequest(BaseModel):
    markdown: str
    output_path: str


# ── File ops ───────────────────────────────────────────────────────────────

@app.post("/files/write")
def write_file(req: WriteRequest):
    path = _resolve(req.path)
    if path.exists() and path.stat().st_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File size exceeds limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(req.content, encoding=req.encoding)
    return {"written": str(path.relative_to(WORKSPACE_ROOT)), "bytes": len(req.content.encode())}


@app.get("/files/read")
def read_file(path: str):
    p = _resolve(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if p.stat().st_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large to read inline")
    return {"path": str(p.relative_to(WORKSPACE_ROOT)), "content": p.read_text(errors="replace")}


@app.delete("/files/delete")
def delete_path(path: str):
    p = _resolve(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return {"deleted": str(p.relative_to(WORKSPACE_ROOT))}


@app.post("/files/mkdir")
def make_dir(req: MkdirRequest):
    p = _resolve(req.path)
    p.mkdir(parents=True, exist_ok=True)
    return {"created": str(p.relative_to(WORKSPACE_ROOT))}


@app.post("/files/move")
def move_path(req: MoveRequest):
    src = _resolve(req.src)
    dst = _resolve(req.dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"moved": {"from": req.src, "to": req.dst}}


@app.get("/files/tree")
def list_tree(path: str = ".", depth: int = 4):
    root = _resolve(path)
    if not root.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    lines: list[str] = []

    def _walk(p: Path, prefix: str, level: int):
        if level > depth:
            return
        try:
            entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
        except PermissionError:
            return
        for i, entry in enumerate(entries):
            connector = "└── " if i == len(entries) - 1 else "├── "
            lines.append(f"{prefix}{connector}{entry.name}")
            if entry.is_dir():
                extension = "    " if i == len(entries) - 1 else "│   "
                _walk(entry, prefix + extension, level + 1)

    lines.append(str(root.relative_to(WORKSPACE_ROOT)) or ".")
    _walk(root, "", 1)
    return {"tree": "\n".join(lines)}


# ── Search ─────────────────────────────────────────────────────────────────

@app.post("/files/search")
def search_files(req: SearchRequest):
    root = _resolve(req.path)
    results: list[dict] = []
    pattern = re.compile(req.query, re.IGNORECASE)

    for p in root.rglob(req.glob):
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        matches = []
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                matches.append({"line": lineno, "text": line.rstrip()})
        if matches:
            results.append({
                "file": str(p.relative_to(WORKSPACE_ROOT)),
                "matches": matches[:20],
            })
        if len(results) >= req.max_results:
            break

    return {"query": req.query, "results": results, "total_files": len(results)}


# ── Code execution ─────────────────────────────────────────────────────────

@app.post("/execute")
def execute_code(req: ExecRequest):
    timeout = min(req.timeout, MAX_EXEC_TIMEOUT)

    if req.language == "python":
        cmd = ["python3", "-c", req.code]
    elif req.language == "bash":
        cmd = ["bash", "-c", req.code]
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported language: {req.language}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(WORKSPACE_ROOT),
            env={**os.environ, "HOME": "/tmp"},
        )
        return {
            "stdout": result.stdout[:8000],
            "stderr": result.stderr[:2000],
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail=f"Execution timed out after {timeout}s")


# ── PDF generation ─────────────────────────────────────────────────────────

@app.post("/generate/pdf")
def generate_pdf(req: PdfRequest):
    try:
        import markdown as md
        from weasyprint import HTML
    except ImportError:
        raise HTTPException(status_code=500, detail="PDF generation requires markdown + weasyprint")

    out_path = _resolve(req.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    html_body = md.markdown(req.markdown, extensions=["fenced_code", "tables"])
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: Georgia, serif; max-width: 800px; margin: 40px auto; line-height: 1.6; }}
  pre {{ background: #f4f4f4; padding: 12px; border-radius: 4px; overflow-x: auto; }}
  code {{ font-family: monospace; font-size: 0.9em; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
  th {{ background: #f0f0f0; }}
</style></head>
<body>{html_body}</body></html>"""

    HTML(string=html).write_pdf(str(out_path))
    return {"pdf": str(out_path.relative_to(WORKSPACE_ROOT)), "bytes": out_path.stat().st_size}


# ── Health ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "workspace": str(WORKSPACE_ROOT), "exists": WORKSPACE_ROOT.exists()}
