# Minimal MCP driver for the live shakedown (see SKILL.md).
# Usage: python drive.py <tool> ['{"json":"args"}']
import asyncio, json, sys
from fastmcp import Client

async def call(tool, **args):
    async with Client("http://127.0.0.1:8000/mcp") as c:
        res = await c.call_tool(tool, args)
        print(res.content[0].text)

asyncio.run(call(sys.argv[1], **json.loads(sys.argv[2] if len(sys.argv) > 2 else "{}")))
