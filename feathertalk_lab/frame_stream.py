"""Publish blocking inference incrementally from one worker per PCM packet."""
import asyncio
import concurrent.futures
import threading

async def iter_frames(iterator):
    loop=asyncio.get_running_loop();queue=asyncio.Queue(maxsize=2);stop=threading.Event()
    def put(kind,value):
        if stop.is_set():return False
        future=asyncio.run_coroutine_threadsafe(queue.put((kind,value)),loop)
        while not stop.is_set():
            try:future.result(timeout=.1);return True
            except concurrent.futures.TimeoutError:pass
        future.cancel();return False
    def produce():
        try:
            for item in iterator:
                if not put('frame',item):break
        except Exception as error:put('error',error)
        finally:
            close=getattr(iterator,'close',None)
            if close:close()
            put('done',None)
    task=asyncio.create_task(asyncio.to_thread(produce))
    try:
        while True:
            kind,value=await queue.get()
            if kind=='done':return
            if kind=='error':raise value
            yield value
    finally:
        stop.set()
        await task
