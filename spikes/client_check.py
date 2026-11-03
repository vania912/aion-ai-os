import asyncio, sys, time
from mcp import Client
from mcp.client.stdio import StdioServerParameters

async def main():
    params = StdioServerParameters(command=sys.executable, args=["hello_server.py"])
    async with Client(params) as c:
        tools = await c.list_tools()
        print("tools:", [(t.name, t.annotations.read_only_hint if t.annotations else None) for t in tools.tools])
        res = await c.list_resources()
        print("resources:", [str(r.uri) for r in res.resources])
        tpl = await c.list_resource_templates()
        print("templates:", [t.uri_template for t in tpl.resource_templates])
        pr = await c.list_prompts()
        print("prompts:", [p.name for p in pr.prompts])
        print("call echo:", (await c.call_tool("echo", {"text": "hi"})).content[0].text)
        print("read tmpl:", (await c.read_resource("project://pulse/decisions")).contents[0].text)
        print("get prompt:", (await c.get_prompt("analyze_project", {"project": "pulse"})).messages[0].content.text)
asyncio.run(main())
