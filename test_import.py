import asyncio
from polymarket import AsyncPublicClient, PRODUCTION

async def main():
    client = AsyncPublicClient(environment=PRODUCTION)
    print("Fetching markets...")
    markets = client.list_markets(page_size=5)
    async for m in markets.iter_items():
        print(f"Market: {m.id}, {m.question}")
        break  # just print first to verify

if __name__ == "__main__":
    asyncio.run(main())
