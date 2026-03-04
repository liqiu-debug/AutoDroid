import asyncio
import websockets
import json

async def test():
    uri = "ws://127.0.0.1:8000/ws/run/9?env_id=1"
    async with websockets.connect(uri) as websocket:
        while True:
            try:
                response = await websocket.recv()
                print(response)
            except websockets.exceptions.ConnectionClosed:
                break

asyncio.run(test())
