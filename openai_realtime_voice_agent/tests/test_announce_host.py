"""The announce endpoint binds where it is told (raawr US-014).

On a Linux host behind a reverse proxy it must listen on loopback only; as a
Home Assistant add-on it keeps listening on every interface.
"""
import pytest

from app import announce_http


async def _bound_host(**kwargs):
    async def announce(_message, _device_id):
        return True

    runner = await announce_http.start_announce_server(0, "t", announce, lambda _d: True, **kwargs)
    try:
        return next(iter(runner.sites))._server.sockets[0].getsockname()[0]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_announce_binds_loopback_when_told():
    assert await _bound_host(host="127.0.0.1") == "127.0.0.1"


@pytest.mark.asyncio
async def test_announce_binds_every_interface_by_default():
    assert await _bound_host() == "0.0.0.0"
