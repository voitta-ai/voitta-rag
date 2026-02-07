"""Parser for WebVTT transcript files (Teams meeting transcripts)."""

import re
from pathlib import Path

from .base import BaseParser, ParserResult

# Matches timestamp lines like "00:00:00.000 --> 00:00:05.000"
_TIMESTAMP_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}\.\d{3}")

# Matches speaker tags like "<v John Smith>"
_SPEAKER_RE = re.compile(r"<v\s+([^>]+)>")


class VttParser(BaseParser):
    """Parser for WebVTT (.vtt) transcript files."""

    extensions = [".vtt"]

    def parse(self, file_path: Path) -> ParserResult:
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = file_path.read_text(encoding="utf-8-sig")
            except Exception as e:
                return ParserResult.failure(f"Failed to read VTT file: {e}")
        except Exception as e:
            return ParserResult.failure(f"Failed to read VTT file: {e}")

        lines = text.splitlines()
        segments: list[tuple[str, str]] = []  # (speaker, text)

        for line in lines:
            line = line.strip()

            # Skip empty lines, WEBVTT header, NOTE blocks, cue identifiers, timestamps
            if not line or line.startswith("WEBVTT") or line.startswith("NOTE"):
                continue
            if _TIMESTAMP_RE.match(line):
                continue
            # Skip numeric cue identifiers
            if line.isdigit():
                continue

            # Extract speaker from <v Speaker> tag
            speaker_match = _SPEAKER_RE.search(line)
            if speaker_match:
                speaker = speaker_match.group(1).strip()
                # Remove all <v ...> and </v> tags to get the text
                spoken = re.sub(r"</?v[^>]*>", "", line).strip()
            else:
                speaker = ""
                spoken = line

            if spoken:
                segments.append((speaker, spoken))

        # Merge consecutive segments from the same speaker
        merged: list[tuple[str, str]] = []
        for speaker, text in segments:
            if merged and merged[-1][0] == speaker:
                merged[-1] = (speaker, merged[-1][1] + " " + text)
            else:
                merged.append((speaker, text))

        # Format as markdown
        parts = []
        for speaker, text in merged:
            if speaker:
                parts.append(f"**{speaker}:** {text}")
            else:
                parts.append(text)

        content = "\n\n".join(parts)
        return ParserResult(content=content)
