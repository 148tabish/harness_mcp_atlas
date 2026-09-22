import os
import base64
from pathlib import Path
from fastmcp import FastMCP

mcp = FastMCP("filesystem")
WORKSPACE_ROOT = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))


def _safe(path: str) -> Path:
    """The workspace path `path` names, refusing anything that lands outside it.

    Every tool here takes a path relative to the workspace, and this server is
    mounted into the one the agent talks to. Joining without checking would let
    `../` or a symlink reach the servers' own code and seed data next door.
    Resolving first follows symlinks and collapses `..`, so the containment test
    runs on where the path really goes rather than on how it is spelled.
    """
    root = WORKSPACE_ROOT.resolve()
    target = (root / str(path or "").lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise PermissionError(f"{path!r} is outside the workspace")
    return target


@mcp.tool
async def list_directory(path: str = "") -> list:
    """List files and directories at the given path relative to workspace root."""
    try:
        target = _safe(path)
    except PermissionError as exc:
        return [f"Error: {exc}"]
    if not target.is_dir():
        return [f"Error: {path!r} is not a directory"]
    root = WORKSPACE_ROOT.resolve()
    return sorted([
        str(p.relative_to(root)) + ("/" if p.is_dir() else "")
        for p in target.iterdir()
    ])


@mcp.tool
async def read_text_file(path: str) -> str:
    """Read a text file at the given path relative to workspace root."""
    return _safe(path).read_text(errors="replace")


@mcp.tool
async def write_text_file(path: str, content: str) -> str:
    """Write text content to a file at the given path relative to workspace root."""
    target = _safe(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"Wrote {len(content)} characters to {path}"


@mcp.tool
async def write_binary_file(path: str, content_base64: str) -> str:
    """Write base64-encoded binary content to a file at the given path relative to workspace root."""
    target = _safe(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(content_base64)
    target.write_bytes(data)
    return f"Wrote {len(data)} bytes to {path}"


@mcp.tool
async def create_directory(path: str) -> str:
    """Create a directory (and parents) at the given path relative to workspace root."""
    _safe(path).mkdir(parents=True, exist_ok=True)
    return f"Created directory {path}"


@mcp.tool
async def file_exists(path: str) -> bool:
    """Check if a file or directory exists at the given path relative to workspace root."""
    try:
        return _safe(path).exists()
    except PermissionError:
        return False          # outside the workspace is, from here, nothing at all


@mcp.tool
async def create_image(source_path: str, dest_path: str, width: int = 1200, height: int = 1200) -> str:
    """Convert and resize an image file. Both paths are relative to workspace root. Saves as PNG."""
    from PIL import Image
    src = _safe(source_path)
    dst = _safe(dest_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    img = Image.open(str(src)).convert("RGB")
    img = img.resize((width, height), Image.LANCZOS)
    img.save(str(dst), "PNG")
    return f"Created {width}x{height} PNG at {dest_path}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=8002, host="0.0.0.0")
