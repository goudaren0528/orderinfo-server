import os
import sys
import time
import json
import requests
from auth import AuthManager

# Ensure current directory is in sys.path
sys.path.append(os.getcwd())

def test_token_rotation():
    print("Starting Token Rotation Test...")
    
    # Setup
    SERVER = os.environ.get('LICENSE_SERVER_URL', 'http://127.0.0.1:5005').replace('localhost', '127.0.0.1')
    m = AuthManager(server_url=SERVER)
    
    # 1. Load existing license
    print("Loading license...")
    code = m.load_license()
    if not code:
        print("FAIL: No active license found. Please activate the client first.")
        return
    print(f"PASS: License loaded: {code}")

    # 2. Check if we have a valid config token
    if not m.state.get('config_token'):
        print("INFO: No config token found, fetching one...")
        ok, data = m.fetch_config()
        if not ok:
            print(f"FAIL: Failed to fetch config: {data}")
            return
    
    original_token = m.state.get('config_token')
    print(f"PASS: Current Token: {original_token[:10]}...")

    # 3. Simulate Token Corruption (Memory only)
    print("Simulating Token Corruption...")
    m.state['config_token'] = "INVALID_TOKEN_FOR_TESTING"
    # Don't save to disk if you want to be safe, but AuthManager loads from disk often.
    # To really test, we must ensure save_user_config uses this invalid token.
    # AuthManager.save_user_config uses self.state['config_token'] directly.
    # We need to force save state to make it persist for the call if it reloads? 
    # Actually auth_manager keeps state in memory.
    
    # 4. Trigger Action that requires Token (save_user_config)
    print("Attempting to save config with INVALID token...")
    # This should:
    # 1. Send request with INVALID_TOKEN
    # 2. Receive 401 Unauthorized
    # 3. Trigger internal fetch_config() to get new token
    # 4. Retry request with NEW_TOKEN
    # 5. Succeed
    
    start_time = time.time()
    ok, msg = m.save_user_config({"last_test_ts": int(time.time())})
    duration = time.time() - start_time
    
    if ok:
        new_token = m.state.get('config_token')
        print(f"PASS: Operation successful (took {duration:.2f}s)")
        print(f"PASS: New Token: {new_token[:10]}...")
        
        if new_token != original_token and new_token != "INVALID_TOKEN_FOR_TESTING":
             print("SUCCESS: Token was automatically refreshed!")
        else:
             print("WARN: Token might not have changed? Check logs.")
    else:
        print(f"FAIL: Operation failed: {msg}")

if __name__ == "__main__":
    test_token_rotation()
