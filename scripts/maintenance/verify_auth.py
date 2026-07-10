import requests
import sys

BASE_URL = "http://localhost:8000"

def test_login():
    print("Testing Login...")
    # 1. Login with default admin credentials
    # Backend runs on localhost:8000. Prefix /api is handled by frontend proxy.
    # Direct access should use /auth/token if mounted there.
    response = requests.post(f"{BASE_URL}/auth/token", data={
        "username": "admin",
        "password": "123456"
    })
    
    if response.status_code == 200:
        token = response.json().get("access_token")
        print(f"✅ Login Successful. Token: {token[:10]}...")
        return token
    else:
        print(f"❌ Login Failed: {response.status_code} {response.text}")
        sys.exit(1)

def test_protected_route(token):
    print("\nTesting Protected Route (Create Case)...")
    headers = {"Authorization": f"Bearer {token}"}
    case_data = {
        "name": "Auth Test Case",
        "steps": [],
        "variables": []
    }
    
    response = requests.post(f"{BASE_URL}/cases", json=case_data, headers=headers)
    
    if response.status_code == 200:
        case = response.json()
        print(f"✅ Case Created. ID: {case['id']}, User ID: {case.get('user_id')}")
        return case['id']
    else:
        print(f"❌ Create Case Failed: {response.status_code} {response.text}")
        sys.exit(1)

def test_cleanup(token, case_id):
    print(f"\nCleaning up Case {case_id}...")
    headers = {"Authorization": f"Bearer {token}"}
    requests.delete(f"{BASE_URL}/cases/{case_id}", headers=headers)
    print("✅ Cleanup Complete")

if __name__ == "__main__":
    try:
        # Give uvicorn a moment to reload if needed
        import time
        time.sleep(2)
        
        token = test_login()
        case_id = test_protected_route(token)
        test_cleanup(token, case_id)
        print("\n🎉 All Auth Tests Passed!")
    except requests.exceptions.ConnectionError:
        print("❌ Could not connect to backend. Is it running?")
