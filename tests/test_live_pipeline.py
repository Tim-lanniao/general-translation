import collections
import io
import json
import threading
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
from scipy.signal import resample_poly

from teams_caption.capture import StreamResampler
from teams_caption.engine import AudioRing, CaptionEngine, Job, SR
from teams_caption.translate import _stream_chat


class LiveTranslationTests(unittest.TestCase):
    def test_stream_previews_and_interrupted_response(self):
        def data(delta):
            event = {"choices": [{"delta": delta}]}
            return b"data:" + json.dumps(event).encode() + b"\n\n"

        stream = b"".join(data(d) for d in ({"role": "assistant"}, {"reasoning_content": "internal"},
                                             {"content": "译文："}, {"content": "会议改到"}, {"content": "周二。"}))
        previews = []
        url = "http://localhost/v1/chat/completions"
        with patch("teams_caption.translate.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(stream + b"data: [DONE]\n")
            full, _, _ = _stream_chat(url, {}, on_partial=previews.append)
            self.assertEqual(full, "会议改到周二。")
            self.assertEqual(previews[0], "会议改到")
            opener.return_value.open.return_value = io.BytesIO(stream)
            with self.assertRaises(RuntimeError):
                _stream_chat(url, {})

    def test_preview_cache_and_final_record(self):
        events = []
        cfg = {"context_chars": 160, "topic": "周会", "terms": ""}
        eng = CaptionEngine(cfg, events.append)
        eng.ring = AudioRing(2)
        eng.context_hist = collections.deque()
        eng.mt_key, eng.mt_text, eng.live_id = None, "", None
        eng.recorder = Mock(dir=None)
        eng.translator = Mock()
        def translate(source, context, topic, terms, chars, *, on_partial, cancelled):
            if on_partial:
                on_partial("会议改到")
            return {"translation": "会议改到周二。", "mt_s": 1.0}
        eng.translator.translate.side_effect = translate
        job = Job("final", 1, 0, SR, SR)
        de = "Der Termin ist am Dienstag."
        self.assertEqual(eng._translate(job, "1.0", de, 0, SR)[0], "会议改到周二。")
        self.assertTrue(events[0]["streaming"])
        eng.recorder.add.assert_not_called()
        self.assertTrue(eng._finalize(job, "1.0", de, 0, SR, 0.2))
        eng.translator.translate.assert_called_once()
        cap = eng.recorder.add.call_args.args[0]
        self.assertEqual((cap["state"], cap["zh"], cap["mt_s"]), ("final", "会议改到周二。", 0.0))
        self.assertEqual(sum(e.get("state") == "final" for e in events), 1)
        # Editing a term while the speaker is talking invalidates the translation.
        eng.context_hist.clear()
        eng.cfg["terms"] = "Dienstag"
        eng._translate(job, "1.0", de, 0, SR)
        self.assertEqual(eng.translator.translate.call_count, 2)

    def test_packet_resampling_matches_whole_audio(self):
        rng = np.random.default_rng(17)
        for rate in (16000, 44100, 48000, 96000, 192000):
            with self.subTest(rate=rate):
                audio = rng.normal(size=rate).astype(np.float32)
                resample = StreamResampler(rate)
                chunks = np.array_split(audio, 97)
                actual = np.concatenate([resample(packet) for packet in chunks])
                reference = resample_poly(audio, resample.up, resample.down)
                np.testing.assert_array_equal(actual, reference[:len(actual)])
                self.assertLessEqual(len(reference) - len(actual), resample.margin * 2)

    def test_pending_draft_can_overlap_confirmed_asr(self):
        for same in (False, True):
            with self.subTest(same=same), ThreadPoolExecutor(max_workers=1) as pool:
                eng = CaptionEngine({"terms": "", "topic": "", "context_chars": 160, "beam_size": 5}, Mock())
                eng.draft_pool = pool
                eng.committed, eng.parts = {}, {}
                eng.mt_key, eng.mt_text, eng.live_id, eng.final_asr = None, "", None, None
                eng.context_hist = collections.deque()
                eng.recorder = Mock(dir=None)
                eng.ring = AudioRing(3)
                eng.ring.append(np.zeros(2 * SR, np.float32))
                final = Job("final", 1, 0, 2 * SR, SR)
                draft = Job("provisional", 1, 0, SR)
                endpoint = {"seq": 1, "start": 0, "end": final.end, "speech_end": SR}
                eng.vad = SimpleNamespace(lock=threading.Lock(), finals=collections.deque([endpoint]))
                final_ready = threading.Event()
                de = "Dienstag" if same else "Dienstag nicht"
                def recognize(audio, **options):
                    self.assertEqual(len(audio), 2 * SR)
                    final_ready.set()
                    return de, [], 0.2
                eng._asr = Mock(side_effect=recognize)
                def translate(source, *args, on_partial, cancelled):
                    self.assertTrue(final_ready.wait(2), "final ASR was blocked by draft MT")
                    if cancelled():
                        raise CancelledError()
                    return {"translation": "周二" if source == "Dienstag" else "周二不行", "mt_s": 0.4}
                eng.translator = Mock()
                eng.translator.translate.side_effect = translate
                if same:
                    self.assertEqual(eng._translate_draft(draft, "1.0", "Dienstag", 0)[0], "周二")
                else:
                    with self.assertRaises(CancelledError):
                        eng._translate_draft(draft, "1.0", "Dienstag", 0)
                eng._final(final)
                eng._asr.assert_called_once()
                self.assertEqual(eng.translator.translate.call_count, 1 if same else 2)
                eng.recorder.add.assert_called_once()
                self.assertEqual(eng.recorder.add.call_args.args[0]["zh"], "周二" if same else "周二不行")

    def test_late_worker_keeps_uncommitted_audio(self):
        cfg = {"terms": "", "topic": "", "window_seconds": 12, "update_seconds": 2}
        eng = CaptionEngine(cfg, Mock())
        eng.committed = {1: SR}
        eng.tick_seq, eng.next_tick = 1, 3 * SR
        eng.ring = AudioRing(20)
        eng.ring.append(np.zeros(15 * SR, np.float32))
        eng.vad = SimpleNamespace(lock=threading.Lock(), finals=collections.deque(), seq=1,
                                  active=0, silence=None, pos=15 * SR, last_speech=15 * SR)
        job = eng._next_job()
        self.assertEqual((job.start, job.end), (SR, 13 * SR))

    def test_brief_pause_never_commits_a_speculative_translation(self):
        cfg = {"window_seconds": 12, "update_seconds": 2, "terms": ""}
        events = []
        eng = CaptionEngine(cfg, events.append)
        eng.vad = Mock()
        eng.parts, eng.live_id = {}, None
        eng.ring = AudioRing(12)
        eng.ring.append(np.zeros(11 * SR, np.float32))
        eng._asr = Mock(return_value=("Dienstag.", [(0, 4, "Dienstag."), (5, 10, " Nein.")], 0.2))
        eng._translate_draft = Mock(return_value=("周二。", 0.7))
        eng.recorder = Mock()
        eng._provisional(Job("ending", 2, 0, 11 * SR))
        eng.recorder.add.assert_not_called()
        self.assertTrue(all(e["state"] == "provisional" for e in events))
        self.assertEqual(eng.count, 0)


if __name__ == "__main__":
    unittest.main()
