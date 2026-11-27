import numpy as np
from livekit import rtc

from multi_audio_input import compute_rms_int16, mix_int16_frames


def _make_frame(samples: list[int], sample_rate: int = 24000) -> rtc.AudioFrame:
    arr = np.array(samples, dtype=np.int16)
    return rtc.AudioFrame(
        data=arr.tobytes(),
        sample_rate=sample_rate,
        num_channels=1,
        samples_per_channel=len(samples),
    )


def test_mix_single_frame_retorna_mismo_objeto():
    f1 = _make_frame([100, -200, 300, -400])
    mixed = mix_int16_frames([f1])
    assert mixed is f1


def test_mix_dos_hablantes_suma_y_recorta_sin_overflow():
    f1 = _make_frame([10000, 25000, -25000, 500])
    f2 = _make_frame([5000, 20000, -20000, -500])
    mixed = mix_int16_frames([f1, f2])
    out = np.frombuffer(mixed.data, dtype=np.int16).tolist()
    assert out == [15000, 32767, -32768, 0]


def test_compute_rms_int16_detecta_energia_de_voz():
    silent = _make_frame([0, 0, 0, 0])
    loud = _make_frame([4000, -4000, 4000, -4000])
    assert compute_rms_int16(silent) == 0.0
    assert compute_rms_int16(loud) == 4000.0
