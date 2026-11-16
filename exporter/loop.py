import asyncio


def new_event_loop() -> asyncio.AbstractEventLoop:
    """uvloop's event loop where it is installed, asyncio's otherwise."""
    try:
        import uvloop
    except ImportError:
        return asyncio.new_event_loop()
    loop: asyncio.AbstractEventLoop = uvloop.new_event_loop()
    return loop
