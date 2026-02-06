"""Parser for plain text files and source code."""

from pathlib import Path

from .base import BaseParser, ParserResult


class TextParser(BaseParser):
    """Parser for plain text and source code files - returns content as-is."""

    extensions = [
        # Plain text and documentation
        ".txt",
        ".md",
        ".rst",
        ".adoc",
        # Python
        ".py",
        ".pyw",
        ".pyi",
        # JavaScript / TypeScript
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        # Web
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".vue",
        ".svelte",
        # Data / Config
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        # Java / JVM
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".groovy",
        ".clj",
        ".cljs",
        # C / C++
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".cxx",
        ".hxx",
        # C# / .NET
        ".cs",
        ".fs",
        ".fsx",
        # Systems
        ".go",
        ".rs",
        ".swift",
        ".m",
        ".mm",
        # Scripting
        ".rb",
        ".php",
        ".pl",
        ".pm",
        ".lua",
        ".r",
        ".jl",
        # Shell
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        # Functional
        ".hs",
        ".ml",
        ".mli",
        ".ex",
        ".exs",
        ".erl",
        ".elm",
        ".rkt",
        ".scm",
        ".lisp",
        # Other
        ".sql",
        ".graphql",
        ".gql",
        ".proto",
        ".dart",
        ".nim",
        ".zig",
        ".v",
        ".sol",
        # Build / DevOps
        ".cmake",
        ".gradle",
        ".dockerfile",
        ".tf",
        ".hcl",
    ]

    def parse(self, file_path: Path) -> ParserResult:
        """Read a text file and return content unchanged."""
        try:
            content = file_path.read_text(encoding="utf-8")
            return ParserResult(content=content)
        except UnicodeDecodeError:
            try:
                content = file_path.read_text(encoding="latin-1")
                return ParserResult(content=content)
            except Exception as e:
                return ParserResult.failure(f"Failed to read text file: {e}")
        except Exception as e:
            return ParserResult.failure(f"Failed to read text file: {e}")
