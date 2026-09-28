"""Launch the real app with psycopg's supported Windows event loop."""

import asyncio
import sys

import uvicorn

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if __name__ == "__main__":
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=int(sys.argv[1]))
    asyncio.run(uvicorn.Server(config).serve())
