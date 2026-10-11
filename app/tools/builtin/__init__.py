"""Built-in tools and the factory that registers them."""

from __future__ import annotations

from app.core.config import Settings
from app.services.knowledge import KnowledgeSearch
from app.services.permissions import PERMISSIONS
from app.tools.builtin.calculator import CalculatorTool
from app.tools.builtin.create_ticket import CreateTicketTool
from app.tools.builtin.http_fetch import HttpFetchTool
from app.tools.builtin.memory_tools import MemoryReadTool, MemoryWriteTool
from app.tools.builtin.research import ArxivSearchTool, WebSearchTool, WikipediaSearchTool
from app.tools.builtin.search_knowledge_base import SearchKnowledgeBaseTool
from app.tools.builtin.send_notification import SendNotificationTool
from app.tools.registry import ToolRegistry


def build_tool_registry(
    settings: Settings,
    http_fetch: HttpFetchTool | None = None,
    knowledge: KnowledgeSearch | None = None,
) -> ToolRegistry:
    """Create a registry containing every built-in tool."""
    registry = ToolRegistry(PERMISSIONS, max_output_chars=settings.tool_output_max_chars)
    for tool in (
        SearchKnowledgeBaseTool(knowledge),
        http_fetch or HttpFetchTool(),
        CalculatorTool(),
        CreateTicketTool(),
        SendNotificationTool(),
        MemoryReadTool(),
        MemoryWriteTool(),
        WebSearchTool(),
        WikipediaSearchTool(),
        ArxivSearchTool(),
    ):
        registry.register(tool)
    return registry
