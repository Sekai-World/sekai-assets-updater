import asyncio
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from anyio import Path as AnyioPath

from updater.media import audio as media_audio
from updater.media.acb import extract_acb


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

    def test_extract_acb_keeps_only_requested_cue_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            acb_path = tmp_path / "voice.acb"
            acb_path.write_bytes(b"acb")
            kept_path = tmp_path / "target.wav"
            removed_path = tmp_path / "other.wav"
            kept_path.write_bytes(b"wav")
            removed_path.write_bytes(b"wav")

            with patch(
                "updater.media.acb.cridecoder.decode_acb_to_wav",
                return_value=[kept_path.as_posix(), removed_path.as_posix()],
            ):
                outputs = extract_acb(
                    BytesIO(b"ignored"),
                    tmp_dir,
                    acb_path.as_posix(),
                    cue_name="target",
                )

            self.assertEqual(outputs, [kept_path.as_posix()])
            self.assertTrue(kept_path.exists())
            self.assertFalse(removed_path.exists())


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
                    outputs = await media_audio._process_extracted_audio_file(
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
                    outputs = await media_audio._process_extracted_audio_file(
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
                    outputs = await media_audio._process_extracted_audio_file(
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
