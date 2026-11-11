from .registry import AgentRegistry
from .specialists.browser import BROWSER
from .specialists.coding import CODING
from .specialists.document import DOCUMENT
from .specialists.research import RESEARCH


def default_registry() -> AgentRegistry:
    return AgentRegistry((BROWSER, CODING, RESEARCH, DOCUMENT))
