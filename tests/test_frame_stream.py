import asyncio
import threading
import pytest
from feathertalk_lab.frame_stream import iter_frames

def test_first_frame_does_not_wait_for_remaining_frames():
    second_started=threading.Event();release=threading.Event()
    def frames():
        yield 'first'
        second_started.set()
        assert release.wait(2)
        yield 'second'
    async def check():
        stream=iter_frames(frames())
        assert await asyncio.wait_for(anext(stream),.5)=='first'
        pending=asyncio.create_task(anext(stream))
        await asyncio.to_thread(second_started.wait,.5)
        assert not pending.done()
        release.set();assert await pending=='second'
        with pytest.raises(StopAsyncIteration):await anext(stream)
    asyncio.run(check())

def test_generator_failure_propagates_without_hanging():
    def frames():
        yield 1
        raise ValueError('generation failed')
    async def check():
        stream=iter_frames(frames());assert await anext(stream)==1
        with pytest.raises(ValueError,match='generation failed'):await anext(stream)
    asyncio.run(check())

def test_closing_consumer_releases_the_worker():
    closed=threading.Event()
    def frames():
        try:
            for i in range(10000):yield i
        finally:closed.set()
    async def check():
        stream=iter_frames(frames());assert await anext(stream)==0
        await asyncio.wait_for(stream.aclose(),1)
        assert closed.is_set()
    asyncio.run(check())
