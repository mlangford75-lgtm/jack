from __future__ import annotations

import re as std_re
try:
    import re2 as re
except ImportError:
    import re
from pathlib import Path

import fnmatch
import os
from jack.pillars.eyes import Eyes
from jack.memory.librarian import Librarian
from jack.chassis.contract_validator import ContractValidator


def _parse_gitignore(gitignore_path: Path) -> list[str]:
    """Parses a .gitignore file and returns a list of patterns."""
    patterns = []
    if gitignore_path.exists():
        with gitignore_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
    return patterns

class FileInterceptor:
    """Route supported file payloads into Pillar IV extraction paths and scans project files."""

    document_extensions = frozenset({".txt", ".md", ".pdf"})
    DOCUMENT_EXTENSIONS = document_extensions
    image_extensions = frozenset({".png", ".jpg", ".jpeg"})
    IMAGE_EXTENSIONS = image_extensions

    DEAD_ZONE_NAMES = {
        "jack.vault", 
        ".jack_vault", 
        "config.yaml", 
        "shadow_ledger", 
        ".jack_ledger",
        ".env", 
        ".vault",
        "prompts.lock.json",
        "live_mission.py",
        "diagnose_keys.py",
        "canaries.yaml",
        ".jack",
        "jack"
    }

    def __init__(self, project_root: Path, librarian: Librarian, contract_validator: ContractValidator, eyes: Eyes) -> None:
        """Initialize the interceptor with the project root, Librarian, ContractValidator, and Eyes pillar implementation."""
        self.project_root = project_root
        self.librarian = librarian
        self.contract_validator = contract_validator
        self.eyes = eyes
        self._dead_zone_inodes: set[tuple[int, int]] = set()
        self._initialize_dead_zone_inodes()

    def _initialize_dead_zone_inodes(self) -> None:
        """Scan the project root at boot time to register the inodes of all active dead-zone files."""
        self.DEAD_ZONE_NAMES_LOWER = {name.lower() for name in self.DEAD_ZONE_NAMES}
        
        for root, dirs, files in os.walk(self.project_root):
            current_path = Path(root)
            
            # FIX: Check if this directory or any of its parents is a dead zone
            is_dead_zone = False
            for p in [current_path] + list(current_path.parents):
                if p.name.lower() in self.DEAD_ZONE_NAMES_LOWER:
                    is_dead_zone = True
                    break
                    
            if is_dead_zone:
                try:
                    stat = current_path.resolve().stat()
                    self._dead_zone_inodes.add((stat.st_dev, stat.st_ino))
                except Exception:
                    pass
                # Register ALL files inside the dead zone directory
                for file in files:
                    try:
                        file_path = current_path / file
                        stat = file_path.resolve().stat()
                        self._dead_zone_inodes.add((stat.st_dev, stat.st_ino))
                    except Exception:
                        pass
            else:
                # Standard check for files outside dead zone directories
                for file in files:
                    if file.lower() in self.DEAD_ZONE_NAMES_LOWER:
                        try:
                            file_path = current_path / file
                            stat = file_path.resolve().stat()
                            self._dead_zone_inodes.add((stat.st_dev, stat.st_ino))
                        except Exception:
                            pass

    def _is_dead_zone(self, path: Path) -> bool:
        """Checks if a given path or any of its parents fall within the forbidden Dead Zone."""
        try:
            resolved = path.resolve()
            resolved_name_lower = resolved.name.lower()
            if resolved_name_lower in self.DEAD_ZONE_NAMES_LOWER:
                return True
                
            for parent in resolved.parents:
                if parent.name.lower() in self.DEAD_ZONE_NAMES_LOWER:
                    return True
            
            stat = resolved.stat()
            if (stat.st_dev, stat.st_ino) in self._dead_zone_inodes:
                return True
                
            for parent in resolved.parents:
                try:
                    p_stat = parent.stat()
                    if (p_stat.st_dev, p_stat.st_ino) in self._dead_zone_inodes:
                        return True
                except Exception:
                    pass
                    
            return False
        except Exception:
            return True

    def intercept_prompt_files(self, prompt: str) -> dict[str, str]:
        """Extract and transcribe every @file reference found in a prompt with descriptor pinning."""
        intercepted: dict[str, str] = {}
        for raw in self._iter_file_references(prompt):
            target_path = Path(raw)
            if target_path.is_absolute():
                raise PermissionError(f"Absolute path blocked: {raw}")
                
            resolved = (self.project_root / target_path).resolve()
            if self._is_dead_zone(resolved):
                raise PermissionError(f"Hard invariant [DEAD_ZONE_VIOLATION] in {raw}")
                
            rel = resolved.relative_to(self.project_root)
            suffix = resolved.suffix.lower()
            
            with open(resolved, "rb") as f:
                fd = f.fileno()
                stat_info = os.fstat(fd)
                if (stat_info.st_dev, stat_info.st_ino) in self._dead_zone_inodes:
                    raise PermissionError(f"Hard invariant [DEAD_ZONE_VIOLATION] in {raw}")
                
                f.seek(0)
                if suffix in self.eyes.DOCUMENT_EXTENSIONS:
                    proof = self.eyes.extract_document(rel, file_obj=f)
                elif suffix in self.eyes.IMAGE_EXTENSIONS:
                    proof = self.eyes.extract_image(rel, file_obj=f)
                else:
                    continue

            self.librarian.add_proof(proof)
            intercepted[str(raw)] = proof["markdown"]
        return intercepted

    @staticmethod
    def _iter_file_references(prompt: str) -> tuple[str, ...]:
        """Return normalized file paths declared with ``@file`` prompt references, supporting quoted spaces."""
        # Capture either single/double-quoted strings (allowing spaces) or unquoted whitespace-terminated strings
        pattern = std_re.compile(
            r"@file(?::|=|\s+)(?:['\"](?P<quoted>[^'\"]+)['\"]|(?P<unquoted>[^'\"\s\n\r]+))",
            std_re.IGNORECASE
        )
        paths: list[str] = []
        for match in pattern.finditer(prompt):
            candidate = match.group("quoted") or match.group("unquoted")
            if not candidate:
                continue
            candidate = candidate.strip().rstrip(".,;)")
            if candidate and candidate not in paths:
                paths.append(candidate)
        return tuple(paths)

    def scan_project_files(self, custom_exclusions: list[str] | None = None) -> list[Path]:
        """Recursively scans project files, respecting .gitignore and custom exclusions."""
        all_files: list[Path] = []
        gitignore_patterns = _parse_gitignore(self.project_root / ".gitignore")
        
        dead_zone_patterns = [
            "jack/**",
            "tests/**",
            "step*.py",
            "step*/*.py",
            "*patch*.py",
            "build_full_code_audit.py",
            "autonomous_mapping_mission.py",
            "librarian_dead_zone_audit.py",
            "live_fire_test.py",
            "local_seed.py",
            "tmp_*.py",
            "*Audit*",
            "*audit*",
            "Full_Code_Audit.md",
            "pasted_content_*.txt",
            "high_irq_deliverables/**",
            "supreme_architect_protocol/**",
            "jack_code_only.txt",
            "jack_clean_codebase.txt",
            ".vault/**",
            ".jack_vault",
            "jack.vault",
            ".jack_ledger/**",
            "shadow_ledger/**",
            "*.zip",
            "*.tar",
            "*.tar.gz",
            "*.gz",
            "*.sqlite3",
            "*.db",
            "*.pyc",
            "__pycache__/**",
            "*.egg-info/**",
            "*.dist-info/**",
            "*.lock",
            "*requirements.txt",
            "*.log",
            "*.tmp",
            "*.temp",
            "*~",
            "#*#",
            "*.swp",
            "*.swo",
            "*.bak",
            "*.orig",
            "*.rej",
            "*.pyo",
            "*.pyd",
            "*.so",
            "*.dll",
            "*.exe",
            "*.bin",
            "*.obj",
            "*.o",
            "*.a",
            "*.lib",
            "*.out",
            "*.diff",
            "*.patch",
            "*.vscode/**",
            "*.idea/**",
            "*.git/**",
            "*.DS_Store",
            "*.env",
            "*.venv/**",
            ".jack_bunker/**",
            "*secret*",
            "*key*",
            "*token*",
            "*credential*",
            "*password*",
            "*passphrase*",
            "*api_key*",
            "*private_key*",
            "*public_key*",
            "*certificate*",
            "*cert*",
            "*pem*",
            "*p12*",
            "*jks*",
            "*kdb*",
            "*srl*",
            "*crl*",
            "*csr*",
            "*p7b*",
            "*p7c*",
            "*p8*",
            "*pkcs*",
            "*keystore*",
            "*truststore*",
            "*config.yaml",
            "live_mission.py",
            "diagnose_keys.py",
            ".jack/**",
            "canaries.yaml",
        ]
        
        if custom_exclusions:
            dead_zone_patterns.extend(custom_exclusions)

        all_exclusion_patterns = gitignore_patterns + dead_zone_patterns

        for root, dirs, files in os.walk(self.project_root):
            current_path = Path(root)
            dirs[:] = [
                d for d in dirs 
                if not self._is_excluded(current_path / d, all_exclusion_patterns, is_dir=True)
                and not self._is_dead_zone(current_path / d)
            ]

            for file in files:
                file_path = current_path / file
                if not self._is_excluded(file_path, all_exclusion_patterns):
                    if not self._is_dead_zone(file_path):
                        all_files.append(file_path)
        return all_files

    def vectorize_and_store_files(self) -> None:
        """Scans project files, vectorizes their content, and stores them in the Librarian.
        Excludes files based on dead zone patterns and .gitignore.
        """
        print("FileInterceptor: Scanning and vectorizing project files...")
        try:
            documents = []
            metadatas = []
            for file_path in self.scan_project_files():
                try:
                    if file_path.suffix.lower() not in {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json"}:
                        continue
                    try:
                        content = file_path.read_text(encoding="utf-8").strip()
                    except UnicodeDecodeError:
                        try:
                            content = file_path.read_text(encoding="utf-16").strip()
                        except UnicodeDecodeError:
                            content = file_path.read_text(encoding="utf-8", errors="ignore").strip()
                    if not content:
                        continue
                        
                    # Robust Individual Ingestion Filter: Check safety violations per-file 
                    # to prevent self-indexing collisions (like gauntlet logs) from aborting the boot database
                    violation = self.contract_validator.hard_violation_type(content)
                    if violation:
                        print(f"FileInterceptor: Skipping {file_path.name} due to safety violation: [{violation}]")
                        continue
                    
                    # SEMANTIC UPGRADE: Detect and extract structured XML patterns from Expanded_Knowledge.md (Case-Insensitive Match)
                    if file_path.name.lower() == "expanded_knowledge.md":
                        pattern_matches = std_re.findall(
                            r'(<pattern\s+domain="([^"]+)"\s+name="([^"]+)">.*?</pattern>)',
                            content,
                            std_re.DOTALL | std_re.IGNORECASE
                        )
                        if pattern_matches:
                            print(f"FileInterceptor: Detected Expanded_Knowledge.md. Extracting {len(pattern_matches)} structured pattern cards...")
                            for full_pattern, domain, name in pattern_matches:
                                documents.append(full_pattern.strip())
                                metadatas.append({
                                    "source": str(file_path.relative_to(self.project_root)),
                                    "pattern_domain": domain,
                                    "pattern_name": name,
                                    "is_xml_pattern": True  # Instructs Librarian to keep this card atomic
                                })
                            continue  # Skip standard unparsed file ingestion
                        
                    documents.append(content)
                    metadatas.append({"source": str(file_path.relative_to(self.project_root))})
                except Exception as e:
                    print(f"FileInterceptor: Could not read file {file_path}: {e}")
            
            if documents:
                self.librarian.store_documents(documents=documents, metadatas=metadatas)
                print(f"FileInterceptor: Stored {len(documents)} documents in Librarian.")
            else:
                print("FileInterceptor: No documents to store.")
        except Exception as e:
            print(f"WARNING: File vectorization failed at bootstrap: {e}. Proceeding with unindexed workspace.")

    def _is_excluded(self, path: Path, patterns: list[str], is_dir: bool = False) -> bool:
        """Checks if a given path should be excluded based on patterns."""
        relative_path = path.relative_to(self.project_root).as_posix()
        for pattern in patterns:
            if pattern.endswith("/") and not is_dir:
                continue
            if fnmatch.fnmatch(relative_path, pattern) or fnmatch.fnmatch(path.name, pattern):
                return True
            if is_dir and pattern.endswith("/") and fnmatch.fnmatch(relative_path + "/", pattern):
                return True
        return False

    def generate_dependency_graph(self) -> dict[str, list[str]]:
        """Generates a dependency graph of the project files based on imports."""
        import ast
        
        graph: dict[str, list[str]] = {}
        files = self.scan_project_files()
        
        for file_path in files:
            if file_path.suffix == ".py":
                relative_path = file_path.relative_to(self.project_root).as_posix()
                graph[relative_path] = []
                try:
                    with file_path.open("r", encoding="utf-8") as f:
                        tree = ast.parse(f.read(), filename=str(file_path))
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                graph[relative_path].append(alias.name)
                        elif isinstance(node, ast.ImportFrom):
                            if node.module:
                                graph[relative_path].append(node.module)
                except Exception as e:
                    pass
        return graph

    def route_file(self, filepath: str) -> str:
        """Route one file to the appropriate deterministic or visual extraction path with descriptor pinning."""
        path = Path(filepath)
        if path.is_absolute():
            try:
                path = path.relative_to(self.project_root)
            except ValueError:
                raise PermissionError(f"Hard invariant [PATH_TRAVERSAL] in {path}")
        
        resolved = (self.project_root / path).resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"File does not exist: {resolved}")

        # TOCTOU Protection: Keep the file descriptor open and locked for the entire duration
        with open(resolved, "rb") as f:
            fd = f.fileno()
            stat_info = os.fstat(fd)
            if (stat_info.st_dev, stat_info.st_ino) in self._dead_zone_inodes:
                raise PermissionError(f"Hard invariant [DEAD_ZONE_VIOLATION] in {filepath}")
                
            for parent in resolved.parents:
                try:
                    p_stat = parent.stat()
                    if (p_stat.st_dev, p_stat.st_ino) in self._dead_zone_inodes:
                        raise PermissionError(f"Hard invariant [DEAD_ZONE_VIOLATION] in {filepath}")
                except Exception:
                    pass

            if self._is_dead_zone(resolved):
                raise PermissionError(f"Hard invariant [DEAD_ZONE_VIOLATION] in {filepath}")

            if self.contract_validator.is_path_traversal(resolved):
                raise PermissionError(f"Hard invariant [PATH_TRAVERSAL] in {path}")

            # Read content from the already-opened handle
            f.seek(0)
            content_bytes = f.read()
            content = content_bytes.decode("utf-8", errors="ignore")

            violation = self.contract_validator.hard_violation_type(content)
            if violation:
                raise PermissionError(f"Hard invariant [{violation}] in {path}")

            suffix = resolved.suffix.lower()
            f.seek(0)  # Reset pointer to pass open descriptor stream safely
            
            if suffix in self.eyes.DOCUMENT_EXTENSIONS:
                proof = self.eyes.extract_document(path, file_obj=f)
            elif suffix in self.eyes.IMAGE_EXTENSIONS:
                proof = self.eyes.extract_image(path, file_obj=f)
            else:
                supported = sorted(self.eyes.DOCUMENT_EXTENSIONS | self.eyes.IMAGE_EXTENSIONS)
                raise ValueError(f"Unsupported file extension {suffix!r}. Supported extensions: {supported}")

        self.librarian.add_proof(proof)
        return proof["markdown"]