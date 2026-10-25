"""MptTTS: TTSProvider qua ``app.services.voice.tts`` của MPT (Edge TTS và 13 provider khác)."""

from __future__ import annotations

from pathlib import Path

from studio.contracts import AudioTrack, VoiceSpec, WordTiming
from studio.providers.base import Retryable
from studio.providers.mpt import ensure_home


def to_word_timings(sub_maker) -> list[WordTiming]:
    """edge_tts 7.x có ``cues``; provider khác của MPT dùng cấu trúc cũ ``subs``/``offset`` (100 ns)."""
    if getattr(sub_maker, "cues", None):
        return [WordTiming(start=c.start.total_seconds(), end=c.end.total_seconds(), text=c.content) for c in sub_maker.cues]
    subs, offsets = getattr(sub_maker, "subs", []), getattr(sub_maker, "offset", [])
    return [WordTiming(start=s / 1e7, end=e / 1e7, text=t) for t, (s, e) in zip(subs, offsets)]


class MptTTS:
    name = "mpt"
    model = None
    paid = False      # Edge TTS miễn phí; giọng trả phí của MPT cần lớp unconfirmed riêng trước khi bật

    def __init__(self, config: dict):
        ensure_home(config)

    def synthesize(self, seg_id: str, text: str, voice: VoiceSpec, out: Path) -> AudioTrack:
        from app.services import voice as mpt_voice   # package của MPT tên là "app"

        sub_maker = mpt_voice.tts(text, voice.name, voice.rate, str(out))
        if sub_maker is None or not Path(out).is_file() or Path(out).stat().st_size == 0:
            raise Retryable(f"MPT TTS không trả audio cho {seg_id}")
        return AudioTrack(path=str(out), words=to_word_timings(sub_maker), provider=f"mpt:{voice.name}",
                          model=f"edge-tts:{voice.name}" if mpt_voice.is_azure_v1_voice(voice.name) else voice.name)
