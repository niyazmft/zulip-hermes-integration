# Example: Basic Zulip Bot Setup
# 
# This example shows how to set up a basic Zulip bot that responds to messages.

import os
import asyncio
from src.zulip_adapter import ZulipAdapter
from gateway.config import PlatformConfig, Platform

async def main():
    # Set up environment (or use .env file)
    os.environ["ZULIP_API_KEY"] = "your-api-key-here"
    os.environ["ZULIP_EMAIL"] = "your-bot@niyaz.zulipchat.com"
    os.environ["ZULIP_SITE"] = "https://niyaz.zulipchat.com"
    os.environ["ZULIP_ALLOWED_USERS"] = "user1032616@niyaz.zulipchat.com"
    
    # Create adapter
    config = PlatformConfig(
        platform=Platform.ZULIP,
        name="zulip"
    )
    
    adapter = ZulipAdapter(config)
    
    # Connect and listen
    await adapter.connect()
    
    print("Bot is running. Press Ctrl+C to stop.")
    
    try:
        # Keep running
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        await adapter.disconnect()
        print("Bot stopped.")

if __name__ == "__main__":
    asyncio.run(main())
