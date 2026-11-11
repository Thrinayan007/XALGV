"""Trusted registry of specialist capabilities."""

from .base import AgentDefinition, AgentRole


class AgentRegistry:
    def __init__(self, definitions: tuple[AgentDefinition, ...]):
        self._definitions = {}
        for definition in definitions:
            definition = AgentDefinition.model_validate(definition)
            if definition.name in self._definitions:
                raise ValueError(f"Duplicate specialist: {definition.name}")
            self._definitions[definition.name] = definition

    def require(self, role: AgentRole) -> AgentDefinition:
        if role not in self._definitions:
            raise ValueError(f"Specialist is unavailable: {role}")
        return self._definitions[role]
