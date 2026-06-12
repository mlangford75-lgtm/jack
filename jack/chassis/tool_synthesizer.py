from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field

class ToolManifest(BaseModel):
    """Deterministic contract defining a tool's capabilities and boundaries."""
    name: str = Field(..., description="The exact name of the tool.")
    description: str = Field(..., description="What the tool does and when to use it.")
    arguments: dict[str, str] = Field(default_factory=dict, description="Required arguments and their descriptions.")
    required_permissions: list[str] = Field(default_factory=list, description="Permissions required to execute this tool.")

class SovereignToolLoader:
    """Manages the deterministic registration and loading of allowed tools."""
    
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._registry: dict[str, ToolManifest] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Registers the core Chassis tools available in the quarantine environment."""
        self.register_tool(ToolManifest(
            name="shell",
            description="Executes a bash shell command in the isolated quarantine buffer.",
            arguments={"command": "The bash command to run"},
            required_permissions=["execute_commands"]
        ))
        self.register_tool(ToolManifest(
            name="python_repl",
            description="Executes python code in a secure sandbox.",
            arguments={"code": "The python script to execute"},
            required_permissions=["execute_code"]
        ))
        self.register_tool(ToolManifest(
            name="filesystem",
            description="Reads or writes files to the quarantine buffer.",
            arguments={"path": "The relative path to the file", "content": "The text content to write"},
            required_permissions=["read_files", "write_files"]
        ))
        self.register_tool(ToolManifest(
            name="browser",
            description="Navigates to a URL and returns the page content as Markdown.",
            arguments={"url": "The full HTTP/HTTPS URL to navigate to"},
            required_permissions=["network_access"]
        ))
        self.register_tool(ToolManifest(
            name="image_gen",
            description="Generates an image using the Visual Studio.",
            arguments={"prompt": "The visual description", "path": "The destination filename"},
            required_permissions=["generate_images"]
        ))
        self.register_tool(ToolManifest(
            name="audio_gen",
            description="Generates audio using the Audio Studio.",
            arguments={"prompt": "The audio description", "transcript": "The text to speak", "path": "The destination filename"},
            required_permissions=["generate_audio"]
        ))

    def register_tool(self, manifest: ToolManifest) -> None:
        """Registers a tool manifest deterministically."""
        self._registry[manifest.name.lower()] = manifest

    def load_tools(self, intent: str = "PLAN", *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Returns a list of tool schemas for the Manager's context based on classified intent."""
        normalized_intent = str(intent).strip().upper()
        
        # Enforce strict progressive disclosure boundaries
        allowed_tools = {
            "FAST": {"python_repl", "filesystem"},
            "PLAN": {"python_repl", "filesystem", "image_gen", "audio_gen"},
            "DEEP": {"shell", "browser", "python_repl", "filesystem", "image_gen", "audio_gen"}
        }
        
        target_set = allowed_tools.get(normalized_intent, allowed_tools["PLAN"])
        
        filtered = []
        for manifest in self._registry.values():
            if manifest.name.lower() in target_set:
                filtered.append(manifest.model_dump())
        return filtered
        
    def validate_tool_access(self, tool_name: str, intent: str = "PLAN") -> bool:
        """Checks if a tool is registered and authorized under the current classified intent."""
        normalized_intent = str(intent).strip().upper()
        
        allowed_tools = {
            "FAST": {"python_repl", "filesystem"},
            "PLAN": {"python_repl", "filesystem", "image_gen", "audio_gen"},
            "DEEP": {"shell", "browser", "python_repl", "filesystem", "image_gen", "audio_gen"}
        }
        
        target_set = allowed_tools.get(normalized_intent, allowed_tools["PLAN"])
        
        normalized_name = tool_name.lower()
        # Resolve common aliases and execution targets to prevent proxy evasion
        alias_map = {
            "bash": "shell",
            "command": "shell",
            "sandbox": "python_repl",
            "file": "filesystem",
            "files": "filesystem",
            "local_file": "filesystem",
            "local_filesystem": "filesystem",
            "web_navigator": "browser",
            "web": "browser"
        }
        resolved_name = alias_map.get(normalized_name, normalized_name)
        
        return resolved_name in target_set and resolved_name in self._registry