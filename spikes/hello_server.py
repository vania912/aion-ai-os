import logging, sys
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("aios.spike")
mcp = MCPServer("aios-spike")

@mcp.tool(title="Echo", annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def echo(text: str) -> str:
    """Echo text back."""
    log.info("echo called")
    return text

@mcp.resource("project://demo", mime_type="text/markdown")
def project_demo() -> str:
    """Concrete project resource."""
    return "# demo project"

@mcp.resource("project://{name}/decisions", mime_type="text/markdown")
def project_decisions(name: str) -> str:
    """Templated decisions index."""
    return f"decisions for {name}"

@mcp.prompt()
def analyze_project(project: str) -> str:
    """Analyze a project."""
    return f"Analyze project {project} read-only."

if __name__ == "__main__":
    mcp.run()
