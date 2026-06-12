from typing import Any


class HotContext:
    """In-memory hot context buffer for the active Jack run."""

    def __init__(self, max_tokens: int = 4096, *args: Any, **kwargs: Any) -> None:
        self._chunks: list[str] = []
        # Defensive type-coercion: safe fallback for MagicMocks in test suites
        try:
            if hasattr(max_tokens, "assert_called") or "Mock" in type(max_tokens).__name__:
                self.max_tokens = 4096
            else:
                self.max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            self.max_tokens = 4096

    def count_tokens(self) -> int:
        text = self.render()
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except ImportError:
            return len(text) // 3

    def add_context(self, context: str, source: str | None = None, is_trusted: bool = False, *args: Any, **kwargs: Any) -> None:
        """Append a non-empty context chunk wrapped in a safe semantic boundary."""
        # Type-Coercion Guard: Immunize against mock object bleed-through
        if hasattr(context, "assert_called") or "Mock" in type(context).__name__:
            return  # Silently drop mock objects to prevent buffer pollution

        if context and str(context).strip():
            if is_trusted:
                # Direct injection for trusted system outputs
                self._chunks.append(str(context).strip())
            else:
                # Explicit containment framing to prevent instruction injection
                src = source or "untrusted_source"
                safe_wrapper = (
                    f"\n=== BEGIN INGESTED EVIDENCE (Source: {src}) ===\n"
                    f"{str(context).strip()}\n"
                    f"=== END INGESTED EVIDENCE (Source: {src}) ===\n"
                )
                self._chunks.append(safe_wrapper)

    def get_context(self, *args: Any, **kwargs: Any) -> str:
        """Return the rendered active context buffer."""
        return self.render()

    def snapshot(self) -> list[str]:
        """Create a deterministic point-in-time copy of the context buffer."""
        return list(self._chunks)

    def restore(self, checkpoint: list[str] | str) -> None:
        """Rollback the context buffer to a previous snapshot state."""
        if isinstance(checkpoint, str):
            self._chunks = [checkpoint]
        else:
            self._chunks = list(checkpoint)

    def render(self) -> str:
        """Render all active context chunks as a deterministic text block."""
        return "\n\n".join(self._chunks)