import asyncio
import time
from pathlib import Path
from jack.chassis.step_executor import QuarantineBuffer, StepExecutor
from jack.chassis.contract_validator import ContractValidator

async def run_benchmark():
    print("Running Chassis Sovereignty Benchmark...")
    project_root = Path.cwd()
    validator = ContractValidator(project_root)
    # StepExecutor requires project_root, validator, and tas_judge.
    # We pass None for tas_judge as it's not needed for this benchmark.
    executor = StepExecutor(project_root, validator, None)
    
    start_time = time.perf_counter()
    
    # Simulate staging 5 files (1MB each)
    qb = QuarantineBuffer("bench_run", project_root)
    dummy_data = "A" * 1024 * 1024
    for i in range(5):
        qb.stage_file(Path(f".jack_bench_{i}.txt"), dummy_data)
        
    # Audit and Commit
    await executor.audit_and_commit(qb)
    
    # Cleanup bench files
    for i in range(5):
        (project_root / f".jack_bench_{i}.txt").unlink(missing_ok=True)
        
    end_time = time.perf_counter()
    total_ms = (end_time - start_time) * 1000
    
    print(f"Benchmark Complete.")
    print(f"Total time for Quarantine -> Audit (5MB) -> Commit -> Zeroize: {total_ms:.2f} ms")
    print(f"Per-file overhead: {(total_ms / 5):.2f} ms")

def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "bench":
        asyncio.run(run_benchmark())
    else:
        print("Usage: jack bench")

if __name__ == "__main__":
    main()