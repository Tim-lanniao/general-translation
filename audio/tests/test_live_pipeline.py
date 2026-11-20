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

import audio_caption  # noqa: F401  (puts ../gateway on sys.path when run from a checkout)
from audio_caption.capture import StreamResampler
from audio_caption.engine import AudioRing, CaptionEngine, CausalVAD, Job, SR, sentence_ended
from translation_gateway.client import stream_chat


class LiveTranslationTests(unittest.TestCase):
    def test_stream_previews_and_interrupted_response(self):
        def data(delta):
            event = {"choices": [{"delta": delta}]}
            return b"data:" + json.dumps(event).encode() + b"\n\n"

        stream = b"".join(data(d) for d in ({"role": "assistant"}, {"reasoning_content": "internal"},
                                             {"content": "译"}, {"content": "文："}, {"content": "会"},
                                             {"content": "议改到"}, {"content": "周二。"}))
        previews = []
        url = "http://localhost/v1/chat/completions"
        with patch("translation_gateway.client.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(stream + b"data: [DONE]\n")
            full, _, _ = stream_chat(url, {}, on_partial=previews.append)
            self.assertEqual(full, "会议改到周二。")
            self.assertEqual(previews, ["会", "会议改到", "会议改到周二。"])
            opener.return_value.open.return_value = io.BytesIO(stream + b'data: {"error":"interrupted"}\n\ndata: [DONE]\n')
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                stream_chat(url, {})
            opener.return_value.open.return_value = io.BytesIO(stream)
            with self.assertRaises(RuntimeError):
                stream_chat(url, {})
            opener.return_value.open.return_value = io.BytesIO(stream + b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n')
            with self.assertRaises(RuntimeError):
                stream_chat(url, {})

    def test_preview_cache_and_final_record(self):
        events = []
        cfg = {"context_chars": 160, "topic": "周会", "terms": ""}
        eng = CaptionEngine(cfg, events.append)
        eng.ring = AudioRing(2)
        eng.context_hist = collections.deque()
        eng.mt_key, eng.mt_text, eng.live_id = None, "", None
        eng.recorder = Mock(dir=None)
        eng.translator = Mock()
        def translate(source, context, topic, chars, *, asr_names, on_partial, cancelled):
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
        # ASR-only edits do not change MT. A new shared glossary revision does.
        eng.context_hist.clear()
        eng.cfg["terms"] = "Dienstag"
        eng._translate(job, "1.0", de, 0, SR)
        eng.translator.translate.assert_called_once()
        eng.translator.terms_revision = "edited"  # reported by the gateway after a glossary edit
        eng._translate(job, "1.0", de, 0, SR)
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
                def translate(source, *args, asr_names, on_partial, cancelled):
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

    def make_engine(self):
        cfg = dict(window_seconds=12, update_seconds=2, terms="", topic="", context_chars=160, beam_size=5)
        eng = CaptionEngine(cfg, Mock())
        eng.parts, eng.committed = {}, {}
        eng.live_id = eng.mt_key = eng.final_asr = None
        eng.tick_seq, eng.next_tick = -1, 0
        eng.context_hist = collections.deque()
        eng.ring = AudioRing(16)
        with patch("faster_whisper.vad.get_vad_model") as model:
            model.return_value.session.run.side_effect = lambda _, args: (args["input"][0, -1:], args["h"], args["c"])
            eng.vad = CausalVAD(eng.ring, 600, eng._poke)
        eng.recorder = Mock(dir=None)
        eng.draft_pool = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(eng.draft_pool.shutdown)
        eng.translator = Mock()
        eng.translator.translate.side_effect = lambda de, *args, **kwargs: dict(translation=de, mt_s=0.3)
        return eng

    @staticmethod
    def frames(eng, prob, count=1):
        frame = np.full(512, prob, np.float32)
        for _ in range(count):
            pos = eng.ring.total
            eng.ring.append(frame)
            eng.vad._frame(pos, frame.reshape(1, 512))

    def test_first_quiet_frame_starts_final_quality_work_then_reuses_it_once(self):
        eng = self.make_engine()
        eng._asr = Mock(return_value=("Ist der Termin am Dienstag?", [], 0.2))
        self.frames(eng, 0.8, 34)
        self.frames(eng, 0.1)
        job = eng._next_job()
        self.assertEqual((job.kind, job.end - job.gap), ("ending", 512))
        eng._provisional(job)
        self.frames(eng, 0.1, 10)
        self.assertFalse(eng.vad.finals)
        eng.recorder.add.assert_not_called()
        self.frames(eng, 0.1)
        final = eng._next_job()
        self.assertEqual((final.kind, final.end - final.gap), ("final", round(0.384 * SR)))
        eng._final(final)
        self.assertEqual(eng.count, 1)
        self.assertEqual(eng._asr.call_args.kwargs["beam_size"], 5)
        eng._asr.assert_called_once()
        eng.translator.translate.assert_called_once()

    def test_resumption_and_uncertain_frames_invalidate_punctuation_and_early_asr(self):
        for prob in (0.45, 0.9):
            with self.subTest(prob=prob):
                eng = self.make_engine()
                eng._asr = Mock(side_effect=[("Am Dienstag.", [], 0.1), ("Am Dienstag nicht, erst bei 219 Stück", [], 0.1)])
                self.frames(eng, 0.8, 34)
                self.frames(eng, 0.1)
                first = eng._next_job()
                eng._provisional(first)
                self.frames(eng, 0.1, 6)
                self.frames(eng, prob, 3)
                self.assertFalse(eng._gap_current(first))
                self.frames(eng, 0.1)
                tail = eng._next_job()
                self.assertGreater(tail.gap, first.gap)
                eng._provisional(tail)
                self.frames(eng, 0.1, 17)
                self.assertFalse(eng.vad.finals)
                self.frames(eng, 0.1)
                final = eng._next_job()
                self.assertEqual(final.end - final.gap, 19 * 512)
                eng._final(final)
                eng.recorder.add.assert_called_once()
                self.assertIn("nicht, erst bei 219", eng.recorder.add.call_args.args[0]["de"])
                self.assertEqual(eng._asr.call_count, 2)

    def test_short_draft_beam_one_and_committed_split_beam_five(self):
        eng = self.make_engine()
        self.frames(eng, 0.8, 345)
        eng._asr = Mock(return_value=("Erster Satz. Weiter.", [(0, 4, "Erster Satz."), (5, 9, " Weiter.")], 0.2))
        eng._provisional(Job("provisional", 1, 0, 2 * SR))
        eng.recorder.add.assert_not_called()
        eng._provisional(Job("provisional", 1, 0, 11 * SR))
        self.assertEqual([(c.kwargs["beam_size"], c.kwargs["word_timestamps"]) for c in eng._asr.call_args_list],
                         [(1, False), (5, True)])
        self.assertEqual(eng.recorder.add.call_args.args[0]["de"], "Erster Satz.")

    def test_speech_during_stream_cancels_preview_and_prevents_cache_write(self):
        eng = self.make_engine()
        eng._asr = Mock(return_value=("Zwanzig.", [], .1))
        self.frames(eng, 0.8, 34)
        self.frames(eng, 0.05)
        def interrupted(source, *args, asr_names, on_partial, cancelled):
            on_partial("二")
            self.frames(eng, 0.9)
            self.assertTrue(cancelled())
            on_partial("二十。")
            return {"translation": "二十。", "mt_s": .1}
        eng.translator.translate.side_effect = interrupted
        with self.assertRaises(CancelledError):
            eng._provisional(eng._next_job())
        shown = [c.args[0]["zh"] for c in eng.emit.call_args_list if c.args[0].get("streaming")]
        self.assertEqual(shown, ["二"])
        self.assertIsNone(eng.mt_key)
        eng.recorder.add.assert_not_called()

    def test_early_decode_overlaps_live_translation_before_pause_confirms(self):
        eng = self.make_engine()
        self.frames(eng, .8, 34)
        draft = Job("provisional", 1, 0, eng.ring.total)
        decoded = threading.Event()
        def recognize(audio, **kwargs):
            self.assertFalse(eng.vad.finals)
            self.assertEqual(len(audio), draft.end + 512)
            decoded.set()
            return "Nein, erst am Freitag.", [], .1
        eng._asr = Mock(side_effect=recognize)
        def translate(source, *args, **kwargs):
            self.frames(eng, .05)
            self.assertTrue(decoded.wait(2))
            return {"translation": "周二", "mt_s": .3}
        eng.translator.translate.side_effect = translate
        with self.assertRaises(CancelledError):
            eng._translate_draft(draft, "1.0", "Am Dienstag.", 0)
        fresh = eng._next_job()
        self.assertEqual(eng._endpoint_asr(fresh, 0), ("Nein, erst am Freitag.", 0.0))
        eng._asr.assert_called_once()

    def test_old_punctuation_cannot_confirm_an_unrecognised_tail(self):
        eng = self.make_engine()
        self.frames(eng, .8, 34)
        eng.vad.note_transcript(1, eng.ring.total, "Alles richtig.")
        self.frames(eng, .1, 3)
        self.frames(eng, .9, 4)
        self.frames(eng, .1, 12)
        self.assertFalse(eng.vad.finals)
        self.frames(eng, .1, 7)
        final = eng._next_job()
        eng._asr = Mock(return_value=("Alles richtig? Nein!", [], .3))
        eng._final(final)
        self.assertEqual(len(eng._asr.call_args.args[0]), eng.ring.total)
        self.assertEqual(eng.recorder.add.call_args.args[0]["de"], "Alles richtig? Nein!")

    def test_term_edits_during_pause_revalidate_asr_and_mt_independently(self):
        for change_shared in (True, False):
            with self.subTest(shared=change_shared):
                eng = self.make_engine()
                eng.translator.terms_revision = 1
                eng.cfg["terms"] = "Presse"
                eng.model = Mock()
                eng.model.hf_tokenizer.encode.return_value = SimpleNamespace(ids=[1])
                eng._asr = Mock(return_value=("Die Presse.", [], .1))
                eng.translator.translate.side_effect = lambda *a, **kw: dict(translation=str(eng.translator.terms_revision), mt_s=.2)
                self.frames(eng, 0.8, 34)
                self.frames(eng, 0.1)
                eng._provisional(eng._next_job())
                if change_shared:
                    eng.translator.terms_revision = 2
                else:
                    eng.cfg["terms"] += "\nMesse"
                self.frames(eng, 0.1, 19)
                eng._final(eng._next_job())
                self.assertEqual(eng._asr.call_count, 1 if change_shared else 2)
                self.assertEqual(eng.translator.translate.call_count, 2 if change_shared else 1)
                self.assertEqual(eng.recorder.add.call_args.args[0]["zh"], str(eng.translator.terms_revision))

    def test_only_sentence_end_marks_allow_short_endpoint(self):
        for text in ("Liefern Sie?", "Alles erledigt.“", "Gut!"):
            self.assertTrue(sentence_ended(text))
        for text in ("Oder", "Mit Dr.", "Das ist z. B.", "Am 19.", "Vielleicht ...", "Lieferbar und."):
            self.assertFalse(sentence_ended(text))


if __name__ == "__main__":
    unittest.main()
