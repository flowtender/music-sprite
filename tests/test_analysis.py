import asyncio
import shutil

import pytest

from music_sprite.analysis import AnalysisError, Analyzer
from music_sprite.config import AnalysisConfig

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_errors_cross_the_process_boundary_and_a_dead_worker_is_replaced(tmp_path):
    analyzer = Analyzer(AnalysisConfig(enabled=True, device="cpu", model_dir=tmp_path))
    missing = tmp_path / "missing.opus"

    async def attempt():
        await analyzer.analyze(missing)

    async def scenario():
        # An unreadable file fails inside the worker, and the user sees the worker's message.
        with pytest.raises(AnalysisError, match="ffmpeg could not decode"):
            await attempt()
        # The OS killing the worker (out of memory, say) must not break analysis for good.
        for process in list(analyzer._pool._processes.values()):
            process.kill()
        with pytest.raises(AnalysisError, match="process died"):
            await attempt()
        with pytest.raises(AnalysisError, match="ffmpeg could not decode"):
            await attempt()

    try:
        asyncio.run(scenario())
    finally:
        analyzer.close()
