from __future__ import annotations
from pathlib import Path
import hashlib
import base64
import io
from typing import Callable, Any
from jack.chassis.interrupt_handler import StreamingIRQ, SovereignInterrupt
from jack.chassis.contract_validator import ContractValidator
from jack.engines.providers.openai_compatible import EngineMessage

class Eyes:
    SYSTEM_PROMPT = "You are the Eyes pillar. Your job is to extract information from files and provide proofs."
    DOCUMENT_EXTENSIONS = {".txt", ".md", ".pdf"}
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

    def __init__(self, project_root: Path, validator: ContractValidator, irq_factory: Callable[[], StreamingIRQ], engine: Any = None):
        self.project_root = project_root
        self.validator = validator
        self.irq_factory = irq_factory
        self.engine = engine

    def _reject_traversal(self, p: Path):
        if p.is_absolute() or ".." in p.parts:
            raise PermissionError(f"Hard invariant [PATH_TRAVERSAL] in {p}")

    def _audit(self, text: str) -> str:
        irq = self.irq_factory()
        out = []
        try:
            for i in range(0, len(text), 512):
                out.append(irq.push(text[i:i+512]))
            out.append(irq.flush())
            return "".join(out)
        except SovereignInterrupt:
            irq.zeroize()
            raise

    def _proof(self, source: Path, method: str, audited: str) -> dict:
        md = audited
        return {
            "source": str(source.relative_to(self.project_root)),
            "method": method,
            "sha256": hashlib.sha256(md.encode()).hexdigest(),
            "bytes_extracted": len(md.encode()),
            "markdown": md,
        }

    def extract_document(self, path: Path, file_obj: io.BytesIO | io.BufferedReader | None = None) -> dict:
        """Extract a document's content, utilizing descriptor pinning if a file object is provided."""
        self._reject_traversal(path)
        resolved = (self.project_root / path).resolve()
        
        if file_obj is not None:
            if hasattr(file_obj, "seek"):
                file_obj.seek(0)
            raw = file_obj.read()
        else:
            raw = resolved.read_bytes()
            
        suffix = resolved.suffix.lower()
        if suffix in {".txt", ".md"}:
            text = raw.decode("utf-8", errors="ignore")
            method = "text"
        elif suffix == ".pdf":
            import fitz
            doc = fitz.open(stream=raw, filetype="pdf")
            try:
                text = "\n".join(page.get_text() for page in doc)
            finally:
                doc.close()
            method = "pymupdf"
        else:
            raise ValueError(f"Unsupported document type: {suffix}")
            
        try:
            audited = self._audit(text)
        except SovereignInterrupt as e:
            rel = resolved.relative_to(self.project_root)
            raise PermissionError(f"Hard invariant [{e.violation_type}] in {rel}") from e
        return self._proof(resolved, method, audited)

    def extract_image(self, path: Path, file_obj: io.BytesIO | io.BufferedReader | None = None) -> dict:
        """Extract an image's description, utilizing descriptor pinning if a file object is provided."""
        self._reject_traversal(path)
        resolved = (self.project_root / path).resolve()
        
        if file_obj is not None:
            if hasattr(file_obj, "seek"):
                file_obj.seek(0)
            raw_bytes = file_obj.read()
        else:
            raw_bytes = resolved.read_bytes()
        
        if self.engine:
            b64_img = base64.b64encode(raw_bytes).decode("utf-8")
            suffix = resolved.suffix.lower().lstrip('.')
            image_url = f"data:image/{suffix};base64,{b64_img}"
            
            messages = [
                EngineMessage(role="system", content="You are the Eyes pillar. Extract all text and describe the contents of this image in detail. Output only the Markdown description."),
                EngineMessage(role="user", content=[
                    {"type": "text", "text": "Transcribe and describe this image."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]),
            ]
            response = self.engine.complete_messages(
                messages,
                preserve_thinking=False, # Enforce selective reasoning optimization
                max_tokens=1000, # Enforce strict deterministic output ceiling
                temperature=0.0 # Force greedy decoding for transcription
            )
            text = response.content.strip()
            method = "multimodal_ocr"
        else:
            text = f"![{resolved.name}](sha256:{hashlib.sha256(raw_bytes).hexdigest()})\n\n*OCR disabled in Beta*"
            method = "image_stub"

        try:
            audited = self._audit(text)
        except SovereignInterrupt as e:
            rel = resolved.relative_to(self.project_root)
            raise PermissionError(f"Hard invariant [{e.violation_type}] in {rel}") from e
        return self._proof(resolved, method, audited)