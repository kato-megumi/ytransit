"""Test script for Yahoo Transit MCP server - Fujisawa to Akihabara."""
import asyncio
from yahoo_transit_mcp import (
    yahoo_transit_search,
    yahoo_transit_station_info,
)


async def test_route_search():
    print("=" * 60)
    print("TEST 1: Search routes Fujisawa → Akihabara (Markdown)")
    print("=" * 60)
    result = await yahoo_transit_search(origin="fujisawa", destination="akihabara")
    print(result)
    print()

    print("=" * 60)
    print("TEST 2: Search routes Fujisawa → Akihabara (JSON)")
    print("=" * 60)
    result_json = await yahoo_transit_search(
        origin="fujisawa", destination="akihabara", response_format="json"
    )
    print(result_json)
    print()

    print("=" * 60)
    print("TEST 3: Station Info - Fujisawa")
    print("=" * 60)
    station_result = await yahoo_transit_station_info(station_name="fujisawa")
    print(station_result)
    print()


if __name__ == "__main__":
    asyncio.run(test_route_search())
