#!/usr/bin/env python3
"""Run the daily IOO graph enrichment agent."""

import asyncio
import json
import logging

from core.database import close_pool, run_migrations
from aura.agents.ioo_enrichment_agent import get_ioo_enrichment_agent

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    await run_migrations()
    try:
        result = await get_ioo_enrichment_agent().run_daily()
        print(json.dumps(result, default=str))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
