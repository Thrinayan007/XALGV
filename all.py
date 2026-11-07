import os
from pathlib import Path

# Project root directory
ROOT = Path(__file__).parent.resolve()

# Output file
OUTPUT = ROOT / "all.txt"

# Directories to exclude
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}

# File extensions to include
INCLUDE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".md",
    ".txt",
    ".html",
    ".css",
    ".sql",
    ".sh",
    ".ps1",
}

# Individual files to exclude
EXCLUDE_FILES = {
    "all.py",
    "all.txt",
    ".env",
    "package-lock.json",
    "poetry.lock",
}


def should_include(file_path):
    """Check whether a file should be collected."""

    if file_path.name in EXCLUDE_FILES:
        return False

    if file_path.name in {"Dockerfile", "Makefile"}:
        return True

    return file_path.suffix.lower() in INCLUDE_EXTENSIONS


def collect_files():
    """Recursively collect project files."""

    collected = []

    for current_root, dirs, files in os.walk(ROOT):

        # Exclude unwanted directories
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in EXCLUDE_DIRS
        ]

        for filename in files:

            file_path = Path(current_root) / filename

            if should_include(file_path):
                collected.append(file_path)

    return sorted(collected)


def generate_output(files):
    """Write collected files into all.txt."""

    with open(OUTPUT, "w", encoding="utf-8") as output:

        for file_path in files:

            relative_path = file_path.relative_to(ROOT)

            output.write("\n")
            output.write("=" * 80 + "\n")
            output.write(f"FILE: {relative_path}\n")
            output.write("=" * 80 + "\n\n")

            try:
                content = file_path.read_text(
                    encoding="utf-8",
                    errors="replace"
                )

                output.write(content)
                output.write("\n")

            except Exception as error:
                output.write(
                    f"[ERROR READING FILE: {error}]\n"
                )


def main():

    files = collect_files()

    generate_output(files)

    print(f"Successfully collected {len(files)} files.")
    print(f"Created: {OUTPUT}")


if __name__ == "__main__":
    main()
