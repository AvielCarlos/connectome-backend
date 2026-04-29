"""Run Ora's Integral Synthesis Agent once."""

from __future__ import annotations

import asyncio
import json
import logging

from core.database import close_pool, get_pool
from ora.agents.integral_synthesis_agent import run_integral_synthesis

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    await get_pool()
    try:
        result = await run_integral_synthesis()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
