"""Run this LOCALLY (not on Render) to create ASSISTANT_SESSION for the assistant user account.

    API_ID=... API_HASH=... python generate_session.py

It prompts for the phone number and login code. The printed string grants full access to the
account: store it only in Render's environment settings, never in git or logs.
"""
import asyncio
import os

from pyrogram import Client


async def main() -> None:
    # The client is created inside the running loop (same rule as bot.py).
    async with Client("session-generator", api_id=int(os.environ["API_ID"]),
                      api_hash=os.environ["API_HASH"], in_memory=True) as app:
        print("\nASSISTANT_SESSION=" + await app.export_session_string())


if __name__ == "__main__":
    asyncio.run(main())
