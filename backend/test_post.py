import asyncio
import httpx
from backend.schemas import Step

async def test():
    async with httpx.AsyncClient() as client:
        step = {"action": "input", "selector": "test_id", "selector_type": "resourceId", "value": "{{ PASSWORD }}", "description": "Test replacement"}
        payload = {"step": step, "env_id": 1, "variables": [{"key": "LOCAL_VAR", "value": "local_value"}]}
        res = await client.post("http://127.0.0.1:8000/device/execute_step", json=payload)
        print("Status", res.status_code)
        
asyncio.run(test())
