"""One-time screenshot extraction with a vision model (results cached in code/cache/).

    python code/extract_images.py            # extract screenshots missing from the cache
    python code/extract_images.py --force    # re-extract every screenshot

Requires ANTHROPIC_API_KEY (environment or .env). The main pipeline only reads the cache.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit.dataset import load_dataset  # noqa: E402
from ghost_audit.evidence.images import _sha256, extract_image, load_cache, save_cache  # noqa: E402
from ghost_audit.llm import LLMUnavailable  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="re-extract even if cached")
    parser.add_argument("--model", help="override GHOST_VISION_MODEL")
    args = parser.parse_args(argv)

    cache = load_cache()
    images = [e for e in load_dataset().evidence_files if e.path.suffix.lower() == ".png"]
    todo = [e for e in images if args.force or cache.get(e.file_id, {}).get("sha256") != _sha256(e.path)]
    print(f"{len(images)} screenshots, {len(todo)} to extract")
    for evidence in todo:
        try:
            cache[evidence.file_id] = extract_image(evidence, args.model)
        except LLMUnavailable as exc:
            print(f"cannot extract: {exc}")
            return 2
        records = cache[evidence.file_id]["extraction"].get("records", [])
        print(f"  {evidence.file_id} {evidence.path.name}: {len(records)} record(s)")
        save_cache(cache)  # save after each image so a failure keeps earlier work
    return 0


if __name__ == "__main__":
    sys.exit(main())
