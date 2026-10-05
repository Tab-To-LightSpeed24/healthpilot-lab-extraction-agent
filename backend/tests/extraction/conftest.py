from pathlib import Path

EVAL_SAMPLES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "eval" / "sample_reports"


def sample_bytes(filename: str) -> bytes:
    return (EVAL_SAMPLES_DIR / filename).read_bytes()
