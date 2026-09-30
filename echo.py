"""Short PCM reference buffer for WebRTC acoustic echo cancellation.

给回声消除器保存刚送往扬声器的 PCM；这里只处理内存中的音频，不写磁盘。
"""

from __future__ import annotations


class SpeakerReference:
    """Pair each microphone block with the nearest speaker block.

    扬声器与麦克风都使用 24 kHz、单声道、16 位 PCM。WebRTC AEC 需要长度相同的
    近端（麦克风）与远端（扬声器）块；扬声器暂时无声时用零填充。

    Both producers run as asyncio tasks on one event loop, so push/take need no lock.
    两个任务都在同一事件循环中运行，因此 push/take 无需线程锁。
    """

    def __init__(self, max_bytes: int = 48_000):
        if max_bytes <= 0 or max_bytes % 2:
            raise ValueError("max_bytes must be a positive number of PCM16 bytes")
        self.max_bytes = max_bytes
        self._pending = bytearray()

    def push(self, pcm: bytes) -> None:
        """Append only audio that is about to reach the actual speaker.

        只放入马上要写给扬声器的片段，避免模型提前生成的排队音频污染参考信号。
        """
        if len(pcm) % 2:
            raise ValueError("PCM16 audio must contain whole samples")
        self._pending.extend(pcm)
        overflow = len(self._pending) - self.max_bytes
        if overflow > 0:
            del self._pending[:overflow]

    def take(self, count: int) -> bytes:
        """Return a same-length speaker reference for a microphone block.

        麦克风每读取一块就取等长参考；不足时补静音，让回声消除器继续运行。
        """
        if count < 0 or count % 2:
            raise ValueError("count must be a nonnegative number of PCM16 bytes")
        available = min(count, len(self._pending))
        result = bytes(self._pending[:available])
        del self._pending[:available]
        return result + bytes(count - available)

    def clear(self) -> None:
        """Drop the cancelled response's unplayed reference audio.

        打断时丢弃旧回答尚未匹配的参考音频，避免影响下一段语音。
        """
        self._pending.clear()
