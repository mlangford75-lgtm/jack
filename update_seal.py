import hashlib
import hmac
import json
import os
from pathlib import Path

def main():
    project_root = Path.cwd()
    lock_path = project_root / "jack" / "chassis" / "chassis.lock.json"
    
    if not lock_path.exists():
        print("Error: Could not find jack/chassis/chassis.lock.json")
        return
        
    try:
        lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
        if 'hashes' not in lock_data:
            print("Error: Malformed chassis.lock.json (missing 'hashes')")
            return
            
        print("Recalculating hashes for modified tracked core files...")
        updated_hashes = {}
        for relative_path in lock_data['hashes'].keys():
            file_path = project_root / relative_path
            if not file_path.exists():
                print(f"Warning: Tracked file {relative_path} not found on disk.")
                continue
            
            content_bytes = file_path.read_bytes()
            computed_hash = hashlib.sha256(content_bytes).hexdigest()
            updated_hashes[relative_path] = computed_hash
            print(f" -> {relative_path}: {computed_hash}")
            
        # Re-sign the lockfile using the anchor key
        passphrase = os.environ.get("JACK_VAULT_PASSPHRASE", "default_chassis_anchor_key")
        key = hashlib.sha256(passphrase.encode("utf-8")).digest()
        
        content = json.dumps(updated_hashes, sort_keys=True).encode("utf-8")
        computed_mac = hmac.new(key, content, hashlib.sha256).hexdigest()
        
        new_lock_data = {
            "hashes": updated_hashes,
            "hmac_signature": computed_mac
        }
        
        lock_path.write_text(json.dumps(new_lock_data, indent=2, sort_keys=True), encoding="utf-8")
        print("\n[Codebase Sealing] Successfully re-signed chassis.lock.json with updated hashes.")
        
    except Exception as e:
        print(f"Error during re-signing: {e}")

if __name__ == "__main__":
    main()