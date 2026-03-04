import urllib.request
import json
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

url = "http://127.0.0.1:8000/device/execute_step"
data = {
    "step": {"action": "input", "selector": "com.haier.uhome.uplus:id/tv_login_by_account", "selector_type": "resourceId", "value": "{{ NAME }}", "description": "Login step"},
    "env_id": 1,
    "variables": [{"key": "LOCAL_VAR", "value": "local_value"}]
}
req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, context=ctx) as response:
        print(json.loads(response.read().decode('utf-8'))['result'])
except urllib.error.HTTPError as e:
    print(e.read().decode('utf-8'))
