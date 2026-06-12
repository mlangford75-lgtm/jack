from __future__ import annotations
import hashlib
import hmac
import json
import os
from pathlib import Path
from jack.chassis.interrupt_handler import SovereignInterrupt

def verify_chassis_integrity(project_root: Path) -> None:
    """Enforces cryptographic codebase sealing."""
    active_root = project_root.resolve()
                    
    # Codebase Integrity Seal Verification (HMAC-verified Chassis Lockfile)
    package_root = Path(__file__).parent.parent.parent.resolve()
    lock_path = package_root / "jack" / "chassis" / "chassis.lock.json"
    
    if not lock_path.exists():
        raise SovereignInterrupt("CHASSIS_INTEGRITY_COMPROMISED: Missing chassis.lock.json seal")
        
    try:
        lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
        if 'hmac_signature' not in lock_data or 'hashes' not in lock_data:
            raise SovereignInterrupt("CHASSIS_INTEGRITY_COMPROMISED: Malformed chassis.lock.json")
            
        # Verify the lockfile's own signature using the Vault master passphrase
        passphrase = os.environ.get("JACK_VAULT_PASSPHRASE", "default_chassis_anchor_key")
        key = hashlib.sha256(passphrase.encode("utf-8")).digest()
        
        # Corrected: Referencing the proper lock_data dict variable
        content = json.dumps(lock_data['hashes'], sort_keys=True).encode("utf-8")
        computed_mac = hmac.new(key, content, hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(computed_mac, lock_data['hmac_signature']):
            raise SovereignInterrupt("CHASSIS_INTEGRITY_COMPROMISED: chassis.lock.json signature mismatch")
            
        # Verify every core file (including sovereign_lock.py itself)
        for relative_path, expected_hash in lock_data['hashes'].items():
            file_path = package_root / relative_path
            if not file_path.exists():
                raise SovereignInterrupt(f"CHASSIS_INTEGRITY_COMPROMISED: Missing core file {relative_path}")
                
            content_bytes = file_path.read_bytes()
            computed_hash = hashlib.sha256(content_bytes).hexdigest()
            if computed_hash != expected_hash:
                raise SovereignInterrupt(f"CHASSIS_INTEGRITY_COMPROMISED: Tamper or bloat detected in {relative_path}")
                
    except SovereignInterrupt:
        raise
    except Exception as exc:
        raise SovereignInterrupt(f"CHASSIS_INTEGRITY_COMPROMISED: Validation error: {exc}")