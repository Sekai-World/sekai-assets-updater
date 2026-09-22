import asyncio
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from anyio import Path as AnyioPath

from updater.media import audio as media_audio
from updater.media.acb import decode_acb_bytes, extract_acb


class ExtractAcbTests(unittest.TestCase):
    def test_extract_acb_decodes_directly_to_wav_when_acb_path_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            acb_path = tmp_path / "voice.acb"
            acb_path.write_bytes(b"acb")
            output_path = tmp_path / "voice.wav"

            with patch(
                "updater.media.acb.cridecoder.decode_acb_to_wav",
                return_value=[output_path.as_posix()],
            ) as decode_mock:
                outputs = extract_acb(BytesIO(b"ignored"), tmp_dir, acb_path.as_posix())

            self.assertEqual(outputs, [output_path.as_posix()])
            decode_mock.assert_called_once_with(acb_path.as_posix(), tmp_dir, None)

    def test_extract_acb_renames_output_when_decoder_name_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            acb_path = tmp_path / "voice.acb"
            acb_path.write_bytes(b"acb")
            decoded_path = tmp_path / "generated-name.wav"
            decoded_path.write_bytes(b"wav")

            with patch(
                "updater.media.acb.cridecoder.decode_acb_to_wav",
                return_value=[decoded_path.as_posix()],
            ):
                outputs = extract_acb(
                    BytesIO(b"ignored"),
                    tmp_dir,
                    acb_path.as_posix(),
                    cue_name="requested",
                )

            requested_path = tmp_path / "requested.wav"
            self.assertEqual(outputs, [requested_path.as_posix()])
            self.assertEqual(requested_path.read_bytes(), b"wav")
            self.assertFalse(decoded_path.exists())

    def test_extract_acb_preserves_multiple_tracks_under_requested_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            acb_path = tmp_path / "voice.acb"
            acb_path.write_bytes(b"acb")
            decoded_paths = [tmp_path / "generated-a.wav", tmp_path / "generated-b.wav"]
            decoded_paths[0].write_bytes(b"track-a")
            decoded_paths[1].write_bytes(b"track-b")

            with patch(
                "updater.media.acb.cridecoder.decode_acb_to_wav",
                return_value=[path.as_posix() for path in decoded_paths],
            ):
                outputs = extract_acb(
                    BytesIO(b"ignored"),
                    tmp_dir,
                    acb_path.as_posix(),
                    cue_name="requested",
                )

            expected_paths = [tmp_path / "requested.wav", tmp_path / "requested-2.wav"]
            self.assertEqual(outputs, [path.as_posix() for path in expected_paths])
            self.assertEqual([path.read_bytes() for path in expected_paths], [b"track-a", b"track-b"])
            self.assertFalse(any(path.exists() for path in decoded_paths))

    def test_extract_acb_raises_when_decoder_returns_no_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            acb_path = Path(tmp_dir) / "voice.acb"
            acb_path.write_bytes(b"acb")
            with patch("updater.media.acb.cridecoder.decode_acb_to_wav", return_value=[]):
                with self.assertRaisesRegex(ValueError, "no tracks"):
                    extract_acb(
                        BytesIO(b"ignored"),
                        tmp_dir,
                        acb_path.as_posix(),
                        cue_name="requested",
                    )

    def test_decode_acb_bytes_uses_requested_name_without_filtering(self) -> None:
        tracks = [
            {"name": "generated-a", "extension": "wav", "data": b"track-a"},
            {"name": "generated-b", "extension": "wav", "data": b"track-b"},
        ]
        with patch(
            "updater.media.acb.cridecoder.decode_acb_to_wav_bytes",
            return_value=tracks,
        ):
            outputs = decode_acb_bytes(b"acb", cue_name="requested")

        self.assertEqual(
            outputs,
            [("requested.wav", b"track-a"), ("requested-2.wav", b"track-b")],
        )

    def test_decode_acb_bytes_preserves_decoder_names_without_cue_name(self) -> None:
        tracks = [{"name": "generated", "extension": "wav", "data": b"track"}]
        with patch(
            "updater.media.acb.cridecoder.decode_acb_to_wav_bytes",
            return_value=tracks,
        ):
            outputs = decode_acb_bytes(b"acb")

        self.assertEqual(outputs, [("generated.wav", b"track")])

    def test_acb_decoders_raise_when_no_tracks_are_returned(self) -> None:
        with patch("updater.media.acb.cridecoder.decode_acb_to_wav_bytes", return_value=[]):
            with self.assertRaisesRegex(ValueError, "no tracks"):
                decode_acb_bytes(b"acb", cue_name="requested")

        with tempfile.TemporaryDirectory() as tmp_dir:
            acb_path = Path(tmp_dir) / "voice.acb"
            acb_path.write_bytes(b"acb")
            with patch("updater.media.acb.cridecoder.decode_acb_to_wav", return_value=[]):
                with self.assertRaisesRegex(ValueError, "no tracks"):
                    extract_acb(BytesIO(b"ignored"), tmp_dir, acb_path.as_posix())


class ProcessExtractedAudioFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_wav_input_skips_hca_decode_and_creates_mp3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_dir = AnyioPath(tmp_dir)
            wav_path = Path(tmp_dir) / "voice.wav"
            wav_path.write_bytes(b"wav")
            mp3_path = AnyioPath(tmp_dir) / "voice.mp3"

            async def fake_encode(_input_path, output_path, _config) -> bool:
                await output_path.write_bytes(b"mp3")
                return True

            with patch.object(
                media_audio, "_run_hca_to_wav", new=AsyncMock(return_value=False)
            ) as hca_mock:
                with patch.object(media_audio, "_run_ffmpeg_audio_encode", new=fake_encode):
                    outputs = await media_audio.process_extracted_audio_file(
                        wav_path.as_posix(),
                        save_dir,
                        SimpleNamespace(),
                        asyncio.Semaphore(1),
                    )

            self.assertEqual(outputs, [AnyioPath(wav_path.as_posix()), mp3_path])
            hca_mock.assert_not_awaited()

    async def test_music_wav_input_creates_flac(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            music_dir = Path(tmp_dir) / "music"
            music_dir.mkdir()
            save_dir = AnyioPath(music_dir.as_posix())
            wav_path = music_dir / "song.wav"
            wav_path.write_bytes(b"wav")
            flac_path = AnyioPath(music_dir.as_posix()) / "song.flac"

            async def fake_encode(_input_path, output_path, _config) -> bool:
                await output_path.write_bytes(output_path.suffix.encode())
                return True

            with patch.object(media_audio, "_run_hca_to_wav", new=AsyncMock(return_value=False)):
                with patch.object(media_audio, "_run_ffmpeg_audio_encode", new=fake_encode):
                    outputs = await media_audio.process_extracted_audio_file(
                        wav_path.as_posix(),
                        save_dir,
                        SimpleNamespace(),
                        asyncio.Semaphore(1),
                    )

            self.assertIn(flac_path, outputs)

    async def test_hca_input_still_uses_hca_decode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_dir = AnyioPath(tmp_dir)
            hca_path = Path(tmp_dir) / "voice.hca"
            hca_path.write_bytes(b"hca")

            async def fake_decode(input_path, output_path, _config) -> bool:
                self.assertEqual(input_path, AnyioPath(hca_path.as_posix()))
                await output_path.write_bytes(b"wav")
                return True

            async def fake_encode(_input_path, output_path, _config) -> bool:
                await output_path.write_bytes(b"mp3")
                return True

            with patch.object(media_audio, "_run_hca_to_wav", new=fake_decode) as decode_mock:
                with patch.object(media_audio, "_run_ffmpeg_audio_encode", new=fake_encode):
                    outputs = await media_audio.process_extracted_audio_file(
                        hca_path.as_posix(),
                        save_dir,
                        SimpleNamespace(),
                        asyncio.Semaphore(1),
                    )

            self.assertFalse(hca_path.exists())
            self.assertIn(AnyioPath(tmp_dir) / "voice.wav", outputs)
            self.assertEqual(decode_mock.__name__, "fake_decode")


if __name__ == "__main__":
    unittest.main()
