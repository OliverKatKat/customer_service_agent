"""Bounded MCP session in its own loop, compatible with Windows Celery Selector loops."""
import asyncio
import json
import os
from pathlib import Path
import sys
from datetime import timedelta
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _session(dsn,tenant,customer,order,timeout):
    # Never give the provider business DB credentials, JWT secrets or model keys.
    env={k:v for k,v in os.environ.items() if k.upper() in
         {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA'}}
    env.update(LOGISTICS_DATABASE_URL=dsn,LOGISTICS_TENANT_ID=tenant,
               LOGISTICS_CUSTOMER_ID=customer,LOGISTICS_ORDER_ID=order,
               PYTHONIOENCODING='utf-8',PYTHONUTF8='1')
    params=StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).resolve().parents[1]/'mcp_servers/logistics.py')],env=env)
    async with asyncio.timeout(timeout):
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                discovered=await session.list_tools()
                if {t.name for t in discovered.tools}!={'get_logistics_status'}:
                    raise ValueError('Unexpected provider tools')
                result=await session.call_tool('get_logistics_status',{'order_id':order})
                if result.isError:
                    raise ValueError('Provider returned an error')
                data=result.structuredContent
                if data is None:
                    data=json.loads(result.content[0].text)
                if not isinstance(data,dict) or data.get('status') not in {'ok','not_found','unavailable'}:
                    raise ValueError('Invalid provider result')
                if data['status']=='ok' and (data.get('order_id')!=order or not isinstance(data.get('shipments'),list)):
                    raise ValueError('Unbound provider result')
                return data


def _run(dsn,tenant,customer,order,timeout):
    kwargs={'loop_factory':asyncio.ProactorEventLoop} if sys.platform=='win32' else {}
    return asyncio.run(_session(dsn,tenant,customer,order,timeout),**kwargs)


async def query_logistics(dsn,tenant,customer,order,timeout=12):
    return await asyncio.to_thread(_run,dsn,tenant,customer,order,timeout)
