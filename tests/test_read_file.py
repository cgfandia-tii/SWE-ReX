from pathlib import Path

import pytest

from swerex.runtime.abstract import ReadFileRequest
from swerex.runtime.local import LocalRuntime


@pytest.fixture
def local_runtime():
    return LocalRuntime()


@pytest.fixture
def test_file(tmp_path: Path):
    """Fixture that provides a test file path and cleans up after."""
    file_path = tmp_path / "test.txt"
    yield file_path
    if file_path.exists():
        file_path.unlink()


async def test_read_file_basic(local_runtime: LocalRuntime, test_file: Path):
    """Test basic file reading."""
    test_file.write_text("Hello\nWorld")

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    assert "Hello" in response.content
    assert "World" in response.content
    assert response.total_lines == 2
    assert not response.truncated
    assert not response.is_binary


async def test_read_file_with_line_numbers(local_runtime: LocalRuntime, test_file: Path):
    """Test file reading with line numbers."""
    test_file.write_text("Line 1\nLine 2\nLine 3")

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file), line_numbers=True))

    assert "00001| Line 1" in response.content
    assert "00002| Line 2" in response.content
    assert "00003| Line 3" in response.content


async def test_read_file_without_line_numbers(local_runtime: LocalRuntime, test_file: Path):
    """Test file reading without line numbers."""
    test_file.write_text("Line 1\nLine 2")

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file), line_numbers=False))

    assert "Line 1\nLine 2" == response.content
    assert "00001|" not in response.content


async def test_read_file_with_offset(local_runtime: LocalRuntime, test_file: Path):
    """Test reading file with offset (pagination)."""
    lines = "\n".join([f"Line {i}" for i in range(1, 11)])
    test_file.write_text(lines)

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file), offset=5, limit=3, line_numbers=True))

    assert "00006| Line 6" in response.content
    assert "00007| Line 7" in response.content
    assert "00008| Line 8" in response.content
    assert "Line 1" not in response.content
    assert response.total_lines == 10
    assert response.truncated
    assert response.lines_shown == (6, 8)


async def test_read_file_with_limit(local_runtime: LocalRuntime, test_file: Path):
    """Test reading file with limit."""
    lines = "\n".join([f"Line {i}" for i in range(1, 101)])
    test_file.write_text(lines)

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file), limit=10, line_numbers=True))

    assert "00001| Line 1" in response.content
    assert "00010| Line 10" in response.content
    assert "Line 11" not in response.content
    assert response.truncated
    assert response.total_lines == 100


async def test_read_file_large_file_truncation(local_runtime: LocalRuntime, test_file: Path):
    """Test that large files are automatically truncated."""
    lines = "\n".join([f"Line {i}" for i in range(1, 3001)])
    test_file.write_text(lines)

    # Default limit is 2000
    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    assert response.truncated
    assert response.total_lines == 3000
    assert response.lines_shown == (1, 2000)


async def test_read_file_not_found(local_runtime: LocalRuntime, tmp_path: Path):
    """Test reading non-existent file."""
    non_existent = tmp_path / "does_not_exist.txt"

    with pytest.raises(FileNotFoundError, match="File not found"):
        await local_runtime.read_file(ReadFileRequest(path=str(non_existent)))


async def test_read_file_not_found_with_suggestions(local_runtime: LocalRuntime, tmp_path: Path):
    """Test file not found with similar file suggestions."""
    # Create similar files
    (tmp_path / "config.json").write_text("content")
    (tmp_path / "config.yaml").write_text("content")

    # Try to read non-existent but similar file (misspelled: confg.json missing 'i')
    non_existent = tmp_path / "confg.json"

    with pytest.raises(FileNotFoundError, match="Did you mean one of these"):
        await local_runtime.read_file(ReadFileRequest(path=str(non_existent)))


async def test_read_file_binary_detection(local_runtime: LocalRuntime, test_file: Path):
    """Test binary file detection."""
    # Write binary content
    test_file.write_bytes(b"\x00\x01\x02\x03\x04")

    with pytest.raises(ValueError, match="Cannot read binary file"):
        await local_runtime.read_file(ReadFileRequest(path=str(test_file)))


async def test_read_file_binary_by_extension(local_runtime: LocalRuntime, tmp_path: Path):
    """Test binary file detection by extension."""
    binary_file = tmp_path / "test.pyc"
    binary_file.write_bytes(b"some content")

    with pytest.raises(ValueError, match="Cannot read binary file"):
        await local_runtime.read_file(ReadFileRequest(path=str(binary_file)))


async def test_read_file_long_lines_truncated(local_runtime: LocalRuntime, test_file: Path):
    """Test that very long lines are truncated."""
    long_line = "x" * 5000
    test_file.write_text(f"Short line\n{long_line}\nAnother line")

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    # Long line should be truncated
    assert "..." in response.content
    assert len(response.content) < 5100  # Should be much shorter than original


async def test_read_file_empty_file(local_runtime: LocalRuntime, test_file: Path):
    """Test reading empty file."""
    test_file.write_text("")

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    # Empty file still gets one line with line number
    assert response.content == "00001| "
    assert response.total_lines == 1  # Empty file has one empty line
    assert not response.truncated


async def test_read_file_single_line(local_runtime: LocalRuntime, test_file: Path):
    """Test reading single line file."""
    test_file.write_text("Single line")

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    assert "00001| Single line" in response.content
    assert response.total_lines == 1


async def test_read_file_mime_type_detection(local_runtime: LocalRuntime, tmp_path: Path):
    """Test MIME type detection for various file types."""
    test_cases = [
        ("test.py", "text/x-python"),
        ("test.js", "text/javascript"),
        ("test.json", "application/json"),
        ("test.md", "text/markdown"),
        ("test.txt", "text/plain"),
    ]

    for filename, expected_mime in test_cases:
        test_file = tmp_path / filename
        test_file.write_text("content")

        response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

        assert response.mime_type == expected_mime


async def test_read_file_invalid_offset(local_runtime: LocalRuntime, test_file: Path):
    """Test invalid offset handling."""
    test_file.write_text("Line 1\nLine 2")

    # Negative offset
    with pytest.raises(ValueError, match="must be >= 0"):
        await local_runtime.read_file(ReadFileRequest(path=str(test_file), offset=-1))

    # Offset beyond file length
    with pytest.raises(ValueError, match="exceeds file length"):
        await local_runtime.read_file(ReadFileRequest(path=str(test_file), offset=100))


async def test_read_file_with_unicode(local_runtime: LocalRuntime, test_file: Path):
    """Test reading file with unicode content."""
    test_file.write_text("Hello 世界\nCafé ☕\n🚀 Emoji")

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    assert "世界" in response.content
    assert "Café" in response.content
    assert "🚀" in response.content


async def test_read_file_with_tabs(local_runtime: LocalRuntime, test_file: Path):
    """Test reading file with tabs."""
    test_file.write_text("def func():\n\tprint('hello')")

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    assert "\t" in response.content


async def test_read_file_encoding_parameter(local_runtime: LocalRuntime, test_file: Path):
    """Test reading file with specific encoding."""
    # Write UTF-8 content
    test_file.write_text("Hello World", encoding="utf-8")

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file), encoding="utf-8"))

    assert "Hello World" in response.content


async def test_read_file_no_trailing_newline(local_runtime: LocalRuntime, test_file: Path):
    """Test reading file without trailing newline."""
    test_file.write_text("Line 1\nLine 2")  # No trailing newline

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    assert response.total_lines == 2
    assert "Line 1" in response.content
    assert "Line 2" in response.content


async def test_read_file_only_newlines(local_runtime: LocalRuntime, test_file: Path):
    """Test reading file with only newlines."""
    test_file.write_text("\n\n\n")

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    assert response.total_lines == 4  # 3 newlines = 4 lines (including empty ones)


async def test_read_file_windows_line_endings(local_runtime: LocalRuntime, test_file: Path):
    """Test reading file with Windows line endings."""
    test_file.write_text("Line 1\r\nLine 2\r\nLine 3")

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    assert "Line 1" in response.content
    assert "Line 2" in response.content
    assert "Line 3" in response.content


async def test_read_file_mixed_content(local_runtime: LocalRuntime, test_file: Path):
    """Test reading file with mixed content types."""
    content = """#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys

def main():
    print("Hello, World! 世界")
    # TODO: implement more features

if __name__ == "__main__":
    main()
"""
    test_file.write_text(content)

    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    assert "#!/usr/bin/env python3" in response.content
    assert "import os" in response.content
    assert "def main():" in response.content
    assert "世界" in response.content


# Parametrized tests for binary file detection
@pytest.mark.parametrize(
    ("extension", "should_be_binary"),
    [
        (".pyc", True),
        (".so", True),
        (".dll", True),
        (".exe", True),
        (".zip", True),
        (".jpg", True),
        (".png", True),
        (".pdf", True),
        (".py", False),
        (".txt", False),
        (".md", False),
        (".json", False),
    ],
)
async def test_binary_detection_by_extension(
    local_runtime: LocalRuntime, tmp_path: Path, extension: str, should_be_binary: bool
):
    """Test binary detection for various file extensions."""
    test_file = tmp_path / f"test{extension}"
    test_file.write_bytes(b"some content")

    if should_be_binary:
        with pytest.raises(ValueError, match="Cannot read binary file"):
            await local_runtime.read_file(ReadFileRequest(path=str(test_file)))
    else:
        # Should succeed for text extensions
        try:
            await local_runtime.read_file(ReadFileRequest(path=str(test_file)))
        except UnicodeDecodeError:
            # This is ok - we wrote binary data but extension suggests text
            pass


async def test_read_file_pagination_workflow(local_runtime: LocalRuntime, test_file: Path):
    """Test typical pagination workflow."""
    # Create a file with 100 lines
    lines = "\n".join([f"Line {i}" for i in range(1, 101)])
    test_file.write_text(lines)

    # Read first page
    response1 = await local_runtime.read_file(ReadFileRequest(path=str(test_file), offset=0, limit=20))
    assert "00001| Line 1" in response1.content
    assert "00020| Line 20" in response1.content
    assert response1.truncated
    assert response1.lines_shown == (1, 20)

    # Read second page
    response2 = await local_runtime.read_file(ReadFileRequest(path=str(test_file), offset=20, limit=20))
    assert "00021| Line 21" in response2.content
    assert "00040| Line 40" in response2.content
    assert response2.truncated
    assert response2.lines_shown == (21, 40)

    # Read last page
    response3 = await local_runtime.read_file(ReadFileRequest(path=str(test_file), offset=80, limit=20))
    assert "00081| Line 81" in response3.content
    assert "00100| Line 100" in response3.content
    assert not response3.truncated  # Last page, no more content


async def test_read_file_integration_with_edit(local_runtime: LocalRuntime, test_file: Path):
    """Test that read_file output works well with edit_file input."""
    content = """def old_function():
    print("old")
    return True

def another_function():
    pass
"""
    test_file.write_text(content)

    # Read the file
    response = await local_runtime.read_file(ReadFileRequest(path=str(test_file)))

    # Line numbers should be visible
    assert "00001| def old_function():" in response.content
    assert '00002|     print("old")' in response.content

    # This makes it easy to identify the exact text to replace in edit_file
    # User can see exact indentation and context
