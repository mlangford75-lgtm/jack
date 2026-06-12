import tempfile
from pathlib import Path
from typing import Any
import docker
import os
import sys
import io
import subprocess
import jack.chassis.sovereign_constants as consts

class PythonREPL:
    """
    Sovereign Python Sandbox (Prime Edition).
    Executes code inside a hardened, non-networked Docker container.
    Host subprocess execution is allowed as a fallback if configured.
    """

    def __init__(self, timeout: int = 30, *args: Any, **kwargs: Any) -> None:
        self.timeout = timeout
        self.client = None
        self._workspace = tempfile.TemporaryDirectory(prefix="jack_repl_")
        self.workspace_path = Path(self._workspace.name)
        self._docker_available = None
        # Phase 30: Immutable Data Volumes
        self.datasets_path = Path.cwd() / "datasets"

    def _check_docker(self) -> bool:
        if self._docker_available is not None:
            return self._docker_available
        try:
            self.client = docker.from_env()
            self.client.ping()
            self._docker_available = True
        except Exception:
            # Silently mark as unavailable; fail-closed during run()
            self._docker_available = False
        return self._docker_available

    def _ensure_client(self):
        if self.client is None:
            try:
                self.client = docker.from_env()
                # Phase 30: Z3 SMT Solvers & Offline Math Image
                try:
                    self.client.images.get("jack-sandbox:latest")
                except docker.errors.ImageNotFound:
                    # Write the Dockerfile to disk so the Docker SDK can build it correctly
                    df_path = self.workspace_path / "Dockerfile"
                    df_path.write_text("FROM python:3.11-slim\nRUN pip install --no-cache-dir sympy numpy z3-solver mpmath scipy\n", encoding="utf-8")
                    self.client.images.build(path=str(self.workspace_path), tag="jack-sandbox:latest", rm=True)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to initialize Docker or build image: {e}")

    def reset(self) -> None:
        """Scrub the volatile workspace and close the Docker client."""
        if self._workspace:
            try:
                self._workspace.cleanup()
            except Exception:
                pass
            self._workspace = tempfile.TemporaryDirectory(prefix="jack_repl_")
            self.workspace_path = Path(self._workspace.name)
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        self._docker_available = None

    def _ensure_last_print(self, code: str) -> str:
        """Deterministically coerce the last line of the script to print its output."""
        lines = code.strip().split("\n")
        if not lines:
            return code
        last_line = lines[-1].strip()
        if "print" in last_line or "import" in last_line:
            return code
        if not last_line or last_line.startswith("#"):
            return code
        # Avoid wrapping assignments or block definitions
        if "=" in last_line or last_line.endswith(":"):
            return code
        lines[-1] = "print(" + last_line + ")"
        return "\n".join(lines)

    def run(self, code: str, *args: Any, **kwargs: Any) -> str:
        """Execute Python code strictly inside a containerized sandbox or fallback to local subprocess."""
        # Whitepaper requirement: Symbolic math support & Z3 SMT Solvers
        symbolic_header = (
            "import math, itertools, collections, sys, os\n"
            "import wave, struct\n"
            "import numpy as np\n"
            "import scipy\n"
            "import scipy.io\n"
            "import scipy.io.wavfile\n"
            "from scipy.io import wavfile\n"
            "import sympy\n"
            "from sympy import S, simplify, solve, symbols\n"
            "import mpmath\n"
            "mpmath.mp.dps = 64\n"
            "try:\n"
            "    import z3\n"
            "except ImportError:\n"
            "    pass\n\n"
        )
        
        # Apply deterministic print coercion
        coerced_code = self._ensure_last_print(code)
        full_code = symbolic_header + coerced_code

        if not self._check_docker():
            if getattr(consts, "ALLOW_LOCAL_SUBPROCESS_FALLBACK", False):
                # Write the script locally to the workspace
                script_name = "script_fallback.py"
                script_path = self.workspace_path / script_name
                script_path.write_text(full_code, encoding="utf-8")
                
                try:
                    result = subprocess.run(
                        [sys.executable, str(script_path)],
                        capture_output=True,
                        text=True,
                        timeout=self.timeout,
                        cwd=str(self.workspace_path)
                    )
                    if result.returncode != 0:
                        return f"Execution failed with exit code {result.returncode}:\n{result.stderr}"
                    return result.stdout.strip() or "Execution completed with no output."
                except subprocess.TimeoutExpired:
                    return "Execution failed: Local subprocess timed out."
                except Exception as exc:
                    return f"Execution failed: {exc}"
            else:
                # Fail-closed execution. No host subprocess allowed.
                raise RuntimeError("Zero-Network Sandbox required. Docker is not running or not accessible. Local subprocess execution is strictly forbidden by the Chassis.")

        self._ensure_client()
        if self.client is None:
            if getattr(consts, "ALLOW_LOCAL_SUBPROCESS_FALLBACK", False):
                # Write the script locally to the workspace
                script_name = "script_fallback.py"
                script_path = self.workspace_path / script_name
                script_path.write_text(full_code, encoding="utf-8")
                
                try:
                    result = subprocess.run(
                        [sys.executable, str(script_path)],
                        capture_output=True,
                        text=True,
                        timeout=self.timeout,
                        cwd=str(self.workspace_path)
                    )
                    if result.returncode != 0:
                        return f"Execution failed with exit code {result.returncode}:\n{result.stderr}"
                    return result.stdout.strip() or "Execution completed with no output."
                except subprocess.TimeoutExpired:
                    return "Execution failed: Local subprocess timed out."
                except Exception as exc:
                    return f"Execution failed: {exc}"
            else:
                raise RuntimeError("Zero-Network Sandbox required. Docker client initialization failed.")
            
        script_name = "script.py"
        script_path = self.workspace_path / script_name
        script_path.write_text(full_code, encoding="utf-8")

        # Deterministic Kernel Discipline
        env = {"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}

        # Phase 30: Immutable Data Volumes
        volumes = {str(self.workspace_path): {"bind": "/workspace", "mode": "rw"}}
        if self.datasets_path.exists() and self.datasets_path.is_dir():
            volumes[str(self.datasets_path.resolve())] = {"bind": "/datasets", "mode": "ro"}

        try:
            # Hardened Container Configuration
            container = self.client.containers.run(
                image="jack-sandbox:latest",
                command=f"python /workspace/{script_name}",
                volumes=volumes,
                network_mode="none", # NO NETWORK ACCESS
                mem_limit="512m",    # RAM LIMIT
                cpu_period=100000,
                cpu_quota=50000,     # 50% CPU LIMIT
                environment=env,
                working_dir="/workspace",
                detach=True,
                stderr=True,
                stdout=True
            )

            try:
                result = container.wait(timeout=self.timeout)
                exit_code = result.get("StatusCode", 1)
                output = container.logs(stdout=True, stderr=True).decode("utf-8")
                if exit_code != 0:
                    return f"Execution failed with exit code {exit_code}:\n{output}"
                return output.strip() or "Execution completed with no output."
            except Exception as exc:
                raise RuntimeError(f"Zero-Network Sandbox execution failed: {exc}") from exc
            finally:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
        except Exception as exc:
            raise RuntimeError(f"Zero-Network Sandbox execution failed: {exc}") from exc

    def execute_code(self, code: str, *args: Any, **kwargs: Any) -> str:
        return self.run(code, *args, **kwargs)