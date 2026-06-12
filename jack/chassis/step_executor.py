from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import base64
import numpy as np
import hashlib
import json
from typing import Any, Literal, Mapping
from jack.chassis.contract_validator import ContractValidator
from jack.chassis.tas_judge import TASJudge
from jack.chassis.models import StepExecutionResult
from jack.chassis.interrupt_handler import SovereignInterrupt
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, field
from jack.chassis.sovereign_constants import GHOST_LEDGER_CHUNK_SIZE, GHOST_LEDGER_OVERLAP

@dataclass(frozen=True, slots=True)
class StepExecutionContext:
    """Strictly typed context for step execution."""
    run_id: str
    project_root: Path
    quarantine: GhostLedger

class StepStatus(Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

@dataclass(frozen=True, slots=True)
class StepRecord:
    """Represents the execution result of a single plan step."""
    step: int
    tool: str
    status: StepStatus
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class StepHandlerResult:
    """Strictly typed result from a step handler."""
    status: StepStatus
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True, slots=True)
class StagedFile:
    relative_path: Path
    quarantine_path: Path

class GhostLedger:
    """Manages a temporary, isolated directory for executing commands and staging files."""

    def __init__(self, run_id: str, project_root: Path) -> None:
        self.run_id = run_id
        self.project_root = project_root
        self.root = Path(tempfile.mkdtemp(prefix=f"jack_ghost_ledger_{run_id}_"))
        self.files: list[StagedFile] = []
        self._discarded = False

    async def run_command(self, command: str, timeout: int = 60) -> tuple[int, str, str]:
        """Executes a shell command within the ghost ledger and captures output."""
        allowed_keys = {"PATH", "LANG", "LC_ALL", "TERM", "PYTHONPATH"}
        clean_env = {key: val for key, val in os.environ.items() if key.upper() in allowed_keys}
        
        if "PYTHONPATH" not in clean_env:
            clean_env["PYTHONPATH"] = ""

        # FIX: Windows NT compatibility command translation. Maps "python3" to active host python.exe
        if sys.platform == "win32" and "python3 " in command:
            command = command.replace("python3 ", f'"{sys.executable}" ')

        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.root,
            env=clean_env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return process.returncode, stdout.decode(), stderr.decode()
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return 1, "", "Command timed out and was killed."

    def stage_file(self, target_path: Path, content: str | bytes) -> StagedFile:
        """Stages a file within the ghost ledger, enforcing strict pre-write path containment."""
        target_path_obj = Path(target_path)
        
        if target_path_obj.is_absolute():
            raise PermissionError(f"Hard invariant [PATH_TRAVERSAL] detected during staging: {target_path_obj}")
            
        full_quarantine_path = (self.root / target_path_obj).resolve()
        
        if not full_quarantine_path.is_relative_to(self.root.resolve()):
            raise PermissionError(f"Hard invariant [SYMLINK_TRAVERSAL_ATTEMPT] in staging: {target_path_obj}")

        full_quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        
        if isinstance(content, bytes):
            full_quarantine_path.write_bytes(content)
        else:
            full_quarantine_path.write_text(content, encoding="utf-8")
            
        staged_file = StagedFile(relative_path=target_path_obj, quarantine_path=full_quarantine_path)
        self.files.append(staged_file)
        return staged_file

    def metadata(self) -> dict[str, Any]:
        """Returns metadata about the ghost ledger."""
        return {"run_id": self.run_id, "root": str(self.root), "project_root": str(self.project_root), "files": [str(f.relative_path) for f in self.files]}

    def commit(self, target_dir: Path | None = None) -> list[Path]:
        """Commits staged files from the quarantine to the project root with rollback cleanup."""
        dest = target_dir or self.project_root
        dest.mkdir(parents=True, exist_ok=True)
        committed_paths: list[Path] = []
        tmp_files_created: list[Path] = []
        try:
            for staged_file in self.files:
                target_full_path = dest / staged_file.relative_path
                
                try:
                    resolved_parent = target_full_path.parent.resolve(strict=True)
                except FileNotFoundError:
                    resolved_parent = target_full_path.parent.resolve()
                    
                if not resolved_parent.is_relative_to(dest.resolve()):
                    raise PermissionError(f"Hard invariant [SYMLINK_TRAVERSAL_ATTEMPT] in {staged_file.relative_path}")
                    
                target_full_path.parent.mkdir(parents=True, exist_ok=True)
                
                tmp = target_full_path.with_name(f"{target_full_path.name}_{self.run_id}.jacktmp")
                tmp_files_created.append(tmp)
                shutil.copy2(staged_file.quarantine_path, tmp)
                
                fd_tmp = os.open(str(tmp), os.O_RDWR)
                try:
                    os.fsync(fd_tmp)
                finally:
                    os.close(fd_tmp)
                    
                os.replace(tmp, target_full_path)
                
                # FIX: Directory syncing is a POSIX guarantee (ensuring directory metadata entry updates).
                # Windows NT and NTFS handle directory metadata consistency automatically upon file closures,
                # and attempting to open a directory file descriptor on Windows raises a PermissionError.
                if sys.platform != "win32":
                    try:
                        dir_fd = os.open(str(target_full_path.parent), getattr(os, "O_DIRECTORY", os.O_RDONLY))
                        try:
                            os.fsync(dir_fd)
                        finally:
                            os.close(dir_fd)
                    except (AttributeError, OSError):
                        pass
                    
                tmp_files_created.remove(tmp)
                committed_paths.append(staged_file.relative_path)
        except Exception:
            for tmp in tmp_files_created:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
            raise
        finally:
            self.strict_discard()
        return committed_paths

    def strict_discard(self) -> None:
        """Strictly discards the ghost ledger with best-effort zeroization, preventing symlink tracking."""
        if self._discarded:
            return
            
        try:
            # Wrap standard filesystem queries in a try-except to absorb Windows delete-pending locks
            if not self.root.exists() or not self.root.is_dir():
                self._discarded = True
                return
            
            # Enforce followlinks=False to ensure os.walk doesn't dive into symlinked directories
            for dirpath, _, filenames in os.walk(self.root, followlinks=False):
                for filename in filenames:
                    file_path = Path(dirpath) / filename
                    try:
                        # Explicit symlink check prevents is_file() from resolving targets outside the ledger
                        if file_path.is_symlink():
                            file_path.unlink()
                            continue
                            
                        if file_path.is_file():
                            size = file_path.stat().st_size
                            # FIX: Use O_NOFOLLOW to prevent TOCTOU symlink hijacking
                            fd = os.open(file_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                            with open(fd, "r+b", closefd=True) as f:
                                f.write(b"\x00" * size)
                                f.flush()
                                os.fsync(f.fileno())
                    except Exception:
                        pass
                        
            shutil.rmtree(self.root, ignore_errors=True)
        except Exception:
            pass
        finally:
            self._discarded = True

class StepExecutor:
    """Executes individual steps within a plan using a GhostLedger."""

    def __init__(self, project_root: Path, validator: ContractValidator, tas_judge: TASJudge) -> None:
        self.project_root = project_root
        self.validator = validator
        self.tas_judge = tas_judge

    def _get_canonical_args(self, tool: str, inputs: dict[str, Any]) -> str:
        """Returns a lexicographically sorted JSON representation of the tool and its inputs."""
        serialized_inputs = json.dumps(inputs, sort_keys=True)
        canonical_payload = {
            "tool": tool.lower().strip(),
            "inputs_hash": hashlib.sha256(serialized_inputs.encode("utf-8")).hexdigest()
        }
        return json.dumps(canonical_payload, sort_keys=True)

    def _scrub_secrets(self, text: str) -> str:
        """Scrubs any raw secrets or canaries using the DLP shield before logging."""
        scrubbed = text
        if self.validator:
            for name, pattern in self.validator.dlp_patterns:
                try:
                    scrubbed = pattern.sub(f"[REDACTED_{name.upper()}]", scrubbed)
                except Exception:
                    pass
        return scrubbed

    async def audit_and_commit(self, quarantine: GhostLedger, artifacts: dict[str, Any] | None = None) -> list[Path]:
        """Audits staged files for safety before committing them using a sliding window and FFT/Visual DLP."""
        from jack.engines.providers.openai_compatible import EngineMessage

        dead_zones = {
            "jack.vault", ".jack_vault", "config.yaml", "shadow_ledger", 
            ".jack_ledger", ".env", ".vault", "prompts.lock.json", 
            "live_mission.py", "diagnose_keys.py", "canaries.yaml", ".jack"
        }
        
        project_root_resolved = self.project_root.resolve()
        
        for staged in quarantine.files:
            # FIX: Explicitly block symlinks from being audited or committed
            if staged.quarantine_path.is_symlink():
                quarantine.strict_discard()
                raise PermissionError(f"Hard invariant [SYMLINK_IN_QUARANTINE] in {staged.relative_path}")

            rel = staged.relative_path
            
            # Phase 19: Path Traversal Gate (Strict Enforcement)
            if rel.is_absolute() or ".." in str(rel):
                quarantine.strict_discard()
                raise PermissionError(f"Hard invariant [PATH_TRAVERSAL] in {rel}")
                
            # Resolve the destination path to catch symlink evasion
            target_full_path = self.project_root / rel
            try:
                resolved_parent = target_full_path.parent.resolve(strict=True)
            except FileNotFoundError:
                resolved_parent = target_full_path.parent.resolve()
            except Exception as e:
                quarantine.strict_discard()
                raise PermissionError(f"Hard invariant [PATH_RESOLUTION_FAILURE] in {rel}: {e}")

            resolved_dest = resolved_parent / target_full_path.name

            # Check if the resolved destination escapes the project root
            if not resolved_dest.is_relative_to(project_root_resolved):
                quarantine.strict_discard()
                raise PermissionError(f"Hard invariant [SYMLINK_TRAVERSAL_ATTEMPT] in {rel}")

            # Check if the resolved destination enters a Dead Zone
            try:
                resolved_rel = resolved_dest.relative_to(project_root_resolved)
                for part in resolved_rel.parts:
                    if part.lower() in dead_zones:
                        quarantine.strict_discard()
                        raise PermissionError(f"Hard invariant [DEAD_ZONE_WRITE_ATTEMPT] in {rel}")
            except ValueError:
                quarantine.strict_discard()
                raise PermissionError(f"Hard invariant [SYMLINK_TRAVERSAL_ATTEMPT] in {rel}")

            for part in rel.parts:
                if part.lower() in dead_zones:
                    quarantine.strict_discard()
                    raise PermissionError(f"Hard invariant [DEAD_ZONE_WRITE_ATTEMPT] in {rel}")
            
            # Binary & Extension Validation
            suffix = staged.quarantine_path.suffix.lower()
            is_binary_media = False
            is_audio_media = False
            is_image_media = False

            if suffix in {".png", ".jpg", ".jpeg"}:
                is_image_media = True
                is_binary_media = True
            elif suffix in {".wav"}:
                is_audio_media = True
                is_binary_media = True
            elif suffix in {".mp3"}:
                is_binary_media = True

            # 1. Deterministic Audio Frequency Auditing (The Ultrasonic Gate)
            if is_audio_media:
                try:
                    import scipy.io.wavfile
                    framerate, data = scipy.io.wavfile.read(str(staged.quarantine_path))
                    if len(data) > 0:
                        if len(data.shape) > 1:
                            data = data[:, 0] # Convert stereo to mono for audit
                        
                        # Ensure data is float for FFT
                        data = data.astype(np.float32)
                        fft_data = np.abs(np.fft.rfft(data))
                        freqs = np.fft.rfftfreq(len(data), 1.0 / framerate)
                        ultrasonic_mask = freqs > 20000
                        if np.any(ultrasonic_mask):
                            ultrasonic_energy = np.max(fft_data[ultrasonic_mask])
                            audible_energy = np.max(fft_data[~ultrasonic_mask]) if np.any(~ultrasonic_mask) else 0
                            if ultrasonic_energy > (audible_energy * 0.05):
                                quarantine.strict_discard()
                                raise SovereignInterrupt(violation_type="AUDIO_STEGANOGRAPHY_DETECTED")
                except SovereignInterrupt:
                    raise
                except PermissionError:
                    raise
                except Exception as exc:
                    quarantine.strict_discard()
                    raise PermissionError(f"Hard invariant [AUDIO_AUDIT_FAILURE] in {rel}: {exc}")

            # 2. Multimodal Visual DLP Auditing
            elif is_image_media and artifacts:
                eyes = artifacts.get("eyes")
                judge = artifacts.get("judge")
                if eyes and eyes.engine:
                    try:
                        raw_bytes = staged.quarantine_path.read_bytes()
                        b64_img = base64.b64encode(raw_bytes).decode("utf-8")
                        image_url = f"data:image/{suffix.lstrip('.')};base64,{b64_img}"
                        messages = [
                            EngineMessage(role="system", content="Identify any text in this image that looks like an API key, password, or credential. Reply ONLY with 'CLEAN' or 'LEAK'."),
                            EngineMessage(role="user", content=[
                                {"type": "text", "text": "Audit this image."},
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ]),
                        ]
                        verdict = eyes.engine.complete_messages(messages).content
                        if "LEAK" in verdict.upper():
                            quarantine.strict_discard()
                            raise SovereignInterrupt(violation_type="DLP_VIOLATION_VISUAL_LEAK")
                        
                        if judge and judge.engine_provider:
                            judge_prompt = f"Verify visual audit of image {rel}. Verdict was {verdict}."
                            judge_verdict = judge.engine_provider.complete(judge_prompt, system_prompt="You are the Auditor. Reply ONLY with 'SAFE' or 'UNSAFE'.").content
                            if "UNSAFE" in judge_verdict.upper():
                                quarantine.strict_discard()
                                raise SovereignInterrupt(violation_type="DLP_VIOLATION_VISUAL_UNSAFE")
                    except SovereignInterrupt:
                        raise
                    except PermissionError:
                        raise
                    except Exception as exc:
                        quarantine.strict_discard()
                        raise PermissionError(f"Hard invariant [VISUAL_AUDIT_FAILURE] in {rel}: {exc}")

            # 3. Chunked Sliding-Window DLP & Judge Audit for Text
            elif not is_binary_media:
                chunk_size = GHOST_LEDGER_CHUNK_SIZE
                overlap = GHOST_LEDGER_OVERLAP
                lookback = b""
                
                try:
                    with staged.quarantine_path.open("rb") as f_txt:
                        while True:
                            chunk = f_txt.read(chunk_size)
                            if not chunk:
                                break
                            
                            chunk_str = chunk.decode("utf-8", errors="ignore")
                            
                            if lookback:
                                audit_zone = lookback + chunk[:overlap]
                                violation = self.validator.hard_violation_type(audit_zone.decode("utf-8", errors="ignore"))
                                if violation:
                                    quarantine.strict_discard()
                                    raise SovereignInterrupt(violation_type="DLP_VIOLATION_SPLIT_BOUNDARY")
                            
                            violation = self.validator.hard_violation_type(chunk_str)
                            if violation:
                                quarantine.strict_discard()
                                raise SovereignInterrupt(violation_type="DLP_VIOLATION_CHUNK")
                            
                            judge = artifacts.get("judge") if artifacts else None
                            if judge and judge.engine_provider:
                                judge_prompt = f"Review this generated chunk for malicious payloads or logic bombs:\n{chunk_str[:4000]}"
                                verdict = judge.engine_provider.complete(judge_prompt, system_prompt="You are the Auditor. Reply ONLY with 'SAFE' or 'UNSAFE'.").content
                                if "UNSAFE" in verdict.upper():
                                    quarantine.strict_discard()
                                    raise SovereignInterrupt(violation_type="DLP_VIOLATION_JUDGE_UNSAFE")

                            lookback = chunk[-overlap:] if len(chunk) > overlap else chunk
                except SovereignInterrupt:
                    raise
                except PermissionError:
                    raise
                except Exception as e:
                    quarantine.strict_discard()
                    raise PermissionError(f"Hard invariant [AUDIT_READ_FAILURE] in {rel}: {e}")
                
        return quarantine.commit()

    async def execute_step(self, quarantine: GhostLedger, command: str, timeout: int = 60) -> StepExecutionResult:
        """Executes a single command within the provided ghost ledger."""
        returncode, stdout, stderr = await quarantine.run_command(command, timeout)
        return StepExecutionResult(
            records={
                "command": command,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
                "quarantine_metadata": quarantine.metadata(),
            }
        )

    async def execute_plan(self, run_id: str, execution_plan: dict[str, Any], prompt: str, artifacts: dict[str, Any], fail_fast: bool, quarantine: GhostLedger | None = None) -> StepExecutionResult:
        """Executes a sequence of steps defined in a typed execution plan under strict transaction-safe WAL logging."""
        records: dict[str, Any] = {}
        steps = execution_plan.get("steps", [])
        if quarantine is None:
            quarantine = GhostLedger(run_id, self.project_root)
        
        if not steps:
            quarantine.strict_discard()
            return StepExecutionResult(records={"error": "Empty execution plan."})

        session_id = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        
        # Initialize transactional, append-only WAL inside .jack/
        wal_dir = self.project_root / ".jack"
        wal_dir.mkdir(parents=True, exist_ok=True)
        wal_path = wal_dir / "trajectory_wal.jsonl"
        wal_history: dict[str, dict[str, Any]] = {}
        
        # Load existing WAL if present for crash recovery (Idempotency check)
        if wal_path.exists():
            try:
                with wal_path.open("r", encoding="utf-8") as f_wal:
                    for line in f_wal:
                        line = line.strip()
                        if not line:
                            continue
                        entry = json.loads(line)
                        c_args = entry["canonical_args"]
                        wal_history[c_args] = entry
                print(f"StepExecutor: Loaded active Write-Ahead Log (WAL) with {len(wal_history)} completed transactions.")
            except Exception as e:
                print(f"StepExecutor: Warning - Failed to parse WAL: {e}")

        # Retrieve container environment digest for cryptographic lineage verification
        container_digest = "sha256:jack-sandbox-latest"
        repl = artifacts.get("python_repl")
        if repl and repl._check_docker():
            try:
                repl._ensure_client()
                img = repl.client.images.get("jack-sandbox:latest")
                container_digest = img.id
            except Exception:
                pass
        
        try:
            for step_data in steps:
                step_num = step_data.get("step", 0)
                tool = str(step_data.get("tool", "")).lower()
                inputs = step_data.get("inputs", {})

                # Compute lexicographically sorted canonical argument string
                canonical_args_str = self._get_canonical_args(tool, inputs)

                # Active WAL Idempotency Check
                if canonical_args_str in wal_history:
                    cached_entry = wal_history[canonical_args_str]
                    cached_output = cached_entry["output"]
                    cached_status = StepStatus(cached_entry["status"])
                    
                    records[f"step_{step_num}"] = StepRecord(
                        step=step_num,
                        tool=tool,
                        status=cached_status,
                        output=cached_output,
                        metadata={
                            "wal_recovered": True,
                            "execution_hash": cached_entry["execution_hash"],
                            "container_digest": cached_entry["container_digest"]
                        }
                    )
                    
                    # Restore base64-encoded staged files back to active quarantine
                    staged_files_to_restore = cached_entry.get("staged_files", [])
                    for sf in staged_files_to_restore:
                        rel_path = Path(sf["relative_path"])
                        content_bytes = base64.b64decode(sf["content_b64"])
                        quarantine.stage_file(rel_path, content_bytes)
                        
                    print(f"StepExecutor: [WAL RECOVERY] Skipped execution of Step {step_num} ({tool}) due to verified transaction match and restored {len(staged_files_to_restore)} file(s).")
                    continue

                files_before = list(quarantine.files)
                
                tool_loader = artifacts.get("tool_loader")
                intent = artifacts.get("intent", "PLAN")
                if tool_loader and not tool_loader.validate_tool_access(tool, intent):
                    records[f"step_{step_num}"] = StepRecord(
                        step=step_num, tool=tool, status=StepStatus.FAILED, output=f"Unauthorized or unknown tool requested: {tool} under intent {intent}"
                    )
                    if fail_fast:
                        break

                if f"step_{step_num}" in records:
                    curr_rec = records[f"step_{step_num}"]
                    if curr_rec.status == StepStatus.COMPLETED:
                        new_files = [f for f in list(quarantine.files) if f not in files_before]
                        staged_files_data = [{"relative_path": str(f.relative_path), "content_b64": base64.b64encode(f.quarantine_path.read_bytes()).decode("utf-8")} for f in new_files]
                        
                        # Compute execution hash and scrub secrets before logging to the WAL
                        exec_hash = hashlib.sha256((str(curr_rec.output) + str(curr_rec.status.value)).encode("utf-8")).hexdigest()
                        scrubbed_output = self._scrub_secrets(curr_rec.output)
                        scrubbed_args = self._scrub_secrets(canonical_args_str)
                        
                        wal_entry = {
                            "step_id": step_num,
                            "tool_name": tool,
                            "canonical_args": scrubbed_args,
                            "container_digest": container_digest,
                            "execution_hash": exec_hash,
                            "output": scrubbed_output,
                            "status": curr_rec.status.value,
                            "staged_files": staged_files_data  # Save staged files!
                        }
                        
                        try:
                            with wal_path.open("a", encoding="utf-8") as f_wal:
                                f_wal.write(json.dumps(wal_entry, sort_keys=True) + "\n")
                                f_wal.flush()
                                os.fsync(f_wal.fileno())
                        except Exception as e:
                            print(f"StepExecutor: Warning - Failed to commit step to WAL: {e}")
                    continue
                
                if tool in ["shell", "bash", "command"]:
                    command = inputs.get("command", "")
                    if not command:
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output="Missing command input."
                        )
                        if fail_fast: break
                        continue
                        
                    step_result = await self.execute_step(quarantine, command)
                    returncode = step_result.records.get("returncode", 1)
                    stdout = step_result.records.get("stdout", "")
                    stderr = step_result.records.get("stderr", "")
                    
                    output = stdout if returncode == 0 else f"Exit {returncode}\n{stderr}"
                    status = StepStatus.COMPLETED if returncode == 0 else StepStatus.FAILED
                    
                    records[f"step_{step_num}"] = StepRecord(
                        step=step_num,
                        tool=tool,
                        status=status,
                        output=output,
                        metadata={"command": command, "returncode": returncode}
                    )
                    
                    if fail_fast and status == StepStatus.FAILED:
                        break
                        
                elif tool in ["python_repl", "sandbox"]:
                    repl = artifacts.get("python_repl")
                    if not repl:
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output="PythonREPL artifact not provided."
                        )
                        if fail_fast: break
                        continue
                        
                    # Graceful fallback for hallucinated code keys
                    code = str(inputs.get("code", inputs.get("script", inputs.get("command", ""))))
                    if not code:
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output="Missing 'code' input for python_repl tool."
                        )
                        if fail_fast: break
                        continue
                        
                    try:
                        # Capture modification times before run to detect new/changed files
                        before_mtimes = {}
                        if repl.workspace_path.exists():
                            for f in repl.workspace_path.rglob("*"):
                                if f.is_file():
                                    before_mtimes[f] = f.stat().st_mtime

                        output = await asyncio.to_thread(repl.run, code)
                        
                        # Bridge: Extract new or modified files from Sandbox to GhostLedger
                        extracted_files = []
                        if repl.workspace_path.exists():
                            for f in repl.workspace_path.rglob("*"):
                                if f.is_file() and f.name != "script.py":
                                    mtime = f.stat().st_mtime
                                    if f not in before_mtimes or mtime > before_mtimes[f]:
                                        try:
                                            rel_path = f.relative_to(repl.workspace_path)
                                            content_bytes = f.read_bytes()
                                            quarantine.stage_file(rel_path, content_bytes)
                                            extracted_files.append(str(rel_path))
                                        except Exception:
                                            pass
                        
                        if extracted_files:
                            output += f"\n\n[Sandbox Bridge] Extracted artifact(s) to quarantine: {', '.join(extracted_files)}"

                        # REAL FIX: Check if the sandbox execution actually failed
                        if str(output).startswith("Execution failed with exit code"):
                            records[f"step_{step_num}"] = StepRecord(
                                step=step_num, tool=tool, status=StepStatus.FAILED, output=str(output)
                            )
                            if fail_fast: break
                        else:
                            records[f"step_{step_num}"] = StepRecord(
                                step=step_num, tool=tool, status=StepStatus.COMPLETED, output=str(output)
                            )
                    except Exception as e:
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output=str(e)
                        )
                        if fail_fast: break
                        
                elif tool in ["filesystem", "file", "files", "local_file", "local_filesystem"]:
                    # Graceful fallback for hallucinated path keys
                    path = str(inputs.get("path", inputs.get("filename", inputs.get("file", "")))).strip()
                    content = str(inputs.get("content", inputs.get("code", inputs.get("text", ""))))
                    
                    if not path or path == ".":
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output="Missing or invalid 'path' input for filesystem tool."
                        )
                        if fail_fast: break
                        continue
                        
                    try:
                        quarantine.stage_file(Path(path), content)
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.COMPLETED, output=f"Filesystem operation logged for {path}", metadata={"path": path}
                        )
                    except Exception as e:
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output=str(e)
                        )
                        if fail_fast: break

                elif tool == "image_gen":
                    studio = artifacts.get("visual_studio")
                    if not studio:
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output="VisualStudio artifact not provided."
                        )
                        if fail_fast: break
                        continue
                    
                    prompt_input = inputs.get("prompt", "")
                    path = inputs.get("path", f"generated_image_{step_num}.png")
                    
                    try:
                        asset = await asyncio.to_thread(studio.generate, prompt=prompt_input)
                        quarantine.stage_file(Path(path), asset.content)
                        
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, 
                            tool=tool, 
                            status=StepStatus.COMPLETED, 
                            output=f"Image generated and staged at {path}",
                            metadata={"path": path, "asset": asset}
                        )
                    except Exception as e:
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output=str(e)
                        )
                        if fail_fast: break

                elif tool == "audio_gen":
                    studio = artifacts.get("audio_studio")
                    if not studio:
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output="AudioStudio artifact not provided."
                        )
                        if fail_fast: break
                        continue
                    
                    prompt_input = inputs.get("prompt", "")
                    transcript_input = inputs.get("transcript")
                    path = inputs.get("path", f"generated_audio_{step_num}.wav")
                    
                    try:
                        asset = await asyncio.to_thread(studio.generate, prompt=prompt_input, transcript=transcript_input)
                        quarantine.stage_file(Path(path), asset.content)
                        
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, 
                            tool=tool, 
                            status=StepStatus.COMPLETED, 
                            output=f"Audio generated and staged at {path}",
                            metadata={"path": path, "asset": asset}
                        )
                    except Exception as e:
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output=str(e)
                        )
                        if fail_fast: break
                
                elif tool in ["browser", "web_navigator", "web"]:
                    navigator = artifacts.get("web_navigator")
                    if not navigator:
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output="WebNavigator artifact not provided."
                        )
                        if fail_fast: break
                        continue
                    
                    url = inputs.get("url", "")
                    try:
                        output = await asyncio.to_thread(navigator.navigate, url)
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.COMPLETED, output=str(output)
                        )
                    except Exception as e:
                        records[f"step_{step_num}"] = StepRecord(
                            step=step_num, tool=tool, status=StepStatus.FAILED, output=str(e)
                        )
                        if fail_fast: break

                else:
                    records[f"step_{step_num}"] = StepRecord(
                        step=step_num, tool=tool, status=StepStatus.FAILED, output=f"Unauthorized or unknown tool requested: {tool}"
                    )
                    if fail_fast:
                        break

                if f"step_{step_num}" in records:
                    curr_rec = records[f"step_{step_num}"]
                    if curr_rec.status == StepStatus.COMPLETED:
                        new_files = [f for f in list(quarantine.files) if f not in files_before]
                        staged_files_data = [{"relative_path": str(f.relative_path), "content_b64": base64.b64encode(f.quarantine_path.read_bytes()).decode("utf-8")} for f in new_files]
                        
                        # Compute execution hash and scrub secrets before logging to the WAL
                        exec_hash = hashlib.sha256((str(curr_rec.output) + str(curr_rec.status.value)).encode("utf-8")).hexdigest()
                        scrubbed_output = self._scrub_secrets(curr_rec.output)
                        scrubbed_args = self._scrub_secrets(canonical_args_str)
                        
                        wal_entry = {
                            "step_id": step_num,
                            "tool_name": tool,
                            "canonical_args": scrubbed_args,
                            "container_digest": container_digest,
                            "execution_hash": exec_hash,
                            "output": scrubbed_output,
                            "status": curr_rec.status.value,
                            "staged_files": staged_files_data  # Save staged files!
                        }
                        
                        try:
                            with wal_path.open("a", encoding="utf-8") as f_wal:
                                f_wal.write(json.dumps(wal_entry, sort_keys=True) + "\n")
                                f_wal.flush()
                                os.fsync(f_wal.fileno())
                        except Exception as e:
                            print(f"StepExecutor: Warning - Failed to commit step to WAL: {e}")

                        # Mid-run Context budget watermark check
                        hot_context = artifacts.get("hot_context")
                        if hot_context:
                            context_chunk = f"Result from {tool} (Step {step_num}):\n{str(curr_rec.output)[:2000]}"
                            hot_context.add_context(context_chunk, source="mid_run_step_execution", is_trusted=True)
                            curr_rec.metadata["added_mid_run"] = True
                            
                            if hot_context.count_tokens() > hot_context.max_tokens * 0.75:
                                print(f"StepExecutor: Mid-run watermark check exceeded 75% ({hot_context.count_tokens()} tokens). Raising HOT_CONTEXT_WATERMARK_BREACH.")
                                raise SovereignInterrupt("HOT_CONTEXT_WATERMARK_BREACH")
            
            committed_files = await self.audit_and_commit(quarantine, artifacts=artifacts)
            records["committed_files"] = [str(f) for f in committed_files]

            # Clear/Purge transaction log WAL upon successful full plan commit
            if wal_path.exists():
                try:
                    wal_path.unlink()
                    print("StepExecutor: Successfully committed transaction plan. Trajectory WAL purged.")
                except Exception:
                    pass
            return StepExecutionResult(records=records)
        except SovereignInterrupt:
            records["error"] = "SovereignInterrupt raised during execution."
            quarantine.strict_discard()
            raise
        except PermissionError as e:
            records["error"] = str(e)
            quarantine.strict_discard()
            raise
        except Exception as e:
            records["error"] = str(e)
            return StepExecutionResult(records=records)
        finally:
            quarantine.strict_discard()

def recover_orphaned_tmp(project_root: Path) -> int:
    """Purge any .jacktmp files orphaned by a previous SIGKILL."""
    purged = 0
    for tmp in project_root.rglob("*.jacktmp"):
        try:
            tmp.unlink()
            purged += 1
        except Exception:
            pass
    return purged


# Backwards compatibility alias for older test suites and modules
QuarantineBuffer = GhostLedger