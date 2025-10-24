from pathlib import Path

import pytest

from swerex.runtime.abstract import EditFileRequest
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


async def test_edit_file_single_occurrence(local_runtime: LocalRuntime, tmp_path: Path):
    file_path = tmp_path / "test.txt"
    file_path.write_text("Hello world\nThis is a test\nGoodbye world")

    response = await local_runtime.edit_file(
        EditFileRequest(
            path=str(file_path),
            old_text="This is a test",
            new_text="This is modified",
            expected_occurrences=1,
        )
    )

    assert response.occurrences_replaced == 1
    assert file_path.read_text() == "Hello world\nThis is modified\nGoodbye world"
    assert "This is a test" in response.diff
    assert "This is modified" in response.diff


async def test_edit_file_multiple_occurrences(local_runtime: LocalRuntime, tmp_path: Path):
    file_path = tmp_path / "test.txt"
    file_path.write_text("TODO: task 1\nTODO: task 2\nTODO: task 3")

    response = await local_runtime.edit_file(
        EditFileRequest(path=str(file_path), old_text="TODO", new_text="DONE", expected_occurrences=3)
    )

    assert response.occurrences_replaced == 3
    assert file_path.read_text() == "DONE: task 1\nDONE: task 2\nDONE: task 3"


async def test_edit_file_multiline(local_runtime: LocalRuntime, tmp_path: Path):
    file_path = tmp_path / "test.py"
    file_path.write_text(
        """def hello():
    print("world")
    return True

def goodbye():
    print("world")
    return False
"""
    )

    response = await local_runtime.edit_file(
        EditFileRequest(
            path=str(file_path),
            old_text="""def hello():
    print("world")
    return True""",
            new_text="""def hello():
    print("world!")
    return True""",
            expected_occurrences=1,
        )
    )

    assert response.occurrences_replaced == 1
    assert 'print("world!")' in file_path.read_text()


async def test_edit_file_create_new(local_runtime: LocalRuntime, tmp_path: Path):
    file_path = tmp_path / "new.txt"

    response = await local_runtime.edit_file(
        EditFileRequest(path=str(file_path), old_text="", new_text="New content", expected_occurrences=1)
    )

    assert response.occurrences_replaced == 0
    assert file_path.exists()
    assert file_path.read_text() == "New content"


async def test_edit_file_not_found(local_runtime: LocalRuntime, tmp_path: Path):
    file_path = tmp_path / "test.txt"
    file_path.write_text("Hello world")

    with pytest.raises(ValueError, match="old_text not found"):
        await local_runtime.edit_file(
            EditFileRequest(
                path=str(file_path),
                old_text="Not there",
                new_text="Something",
                expected_occurrences=1,
            )
        )


async def test_edit_file_wrong_occurrence_count(local_runtime: LocalRuntime, tmp_path: Path):
    file_path = tmp_path / "test.txt"
    file_path.write_text("Hello world\nHello world")

    with pytest.raises(ValueError, match="Expected 1 occurrence.*but found 2"):
        await local_runtime.edit_file(
            EditFileRequest(
                path=str(file_path),
                old_text="Hello world",
                new_text="Hi world",
                expected_occurrences=1,
            )
        )


async def test_edit_file_create_existing(local_runtime: LocalRuntime, tmp_path: Path):
    file_path = tmp_path / "test.txt"
    file_path.write_text("Existing content")

    with pytest.raises(ValueError, match="file already exists"):
        await local_runtime.edit_file(
            EditFileRequest(path=str(file_path), old_text="", new_text="New content", expected_occurrences=1)
        )


async def test_edit_file_fuzzy_line_trimmed(local_runtime: LocalRuntime, tmp_path: Path):
    file_path = tmp_path / "test.py"
    file_path.write_text(
        """def hello():
    print("world")
    return True
"""
    )

    # Search text has different leading/trailing whitespace
    response = await local_runtime.edit_file(
        EditFileRequest(
            path=str(file_path),
            old_text="""  def hello():
      print("world")
      return True  """,
            new_text="""def hello():
    print("hello!")
    return True""",
            expected_occurrences=1,
        )
    )

    assert response.occurrences_replaced == 1
    assert 'print("hello!")' in file_path.read_text()


async def test_edit_file_creates_parent_dirs(local_runtime: LocalRuntime, tmp_path: Path):
    file_path = tmp_path / "nested" / "dir" / "test.txt"

    response = await local_runtime.edit_file(
        EditFileRequest(path=str(file_path), old_text="", new_text="Content", expected_occurrences=1)
    )

    assert response.occurrences_replaced == 0
    assert file_path.exists()
    assert file_path.read_text() == "Content"


async def test_edit_file_nonexistent(local_runtime: LocalRuntime, tmp_path: Path):
    file_path = tmp_path / "nonexistent.txt"

    with pytest.raises(FileNotFoundError, match="not found"):
        await local_runtime.edit_file(
            EditFileRequest(
                path=str(file_path),
                old_text="something",
                new_text="something else",
                expected_occurrences=1,
            )
        )


# Parametrized tests for special characters and edge cases
@pytest.mark.parametrize(
    ("old_text", "new_text", "description"),
    [
        ("Hello $world", "Hello ${world}", "dollar signs"),
        ('print("Hello")', 'print("Hi")', "quotes"),
        ("Line1\nLine2", "Line1\nModified", "embedded newlines"),
        ("a\tb", "a    b", "tabs to spaces"),
        ("café", "coffee", "unicode characters"),
        ("Hello 世界", "Hello World", "unicode mixed"),
        ("a * b + c", "a * b * c", "special regex chars"),
        ("C:\\path\\to\\file", "C:/path/to/file", "backslashes"),
        ("#!/bin/bash", "#!/usr/bin/env bash", "shebang"),
        ("", "new content", "empty old_text for creation"),
    ],
)
async def test_edit_file_special_chars(
    local_runtime: LocalRuntime, test_file: Path, old_text: str, new_text: str, description: str
):
    """Test editing with various special characters and patterns."""
    # For empty old_text (creation case), file should not exist
    if old_text == "":
        assert not test_file.exists()
    else:
        test_file.write_text(f"Before {old_text} After")

    result = await local_runtime.edit_file(
        EditFileRequest(
            path=str(test_file),
            old_text=old_text,
            new_text=new_text,
            expected_occurrences=1 if old_text else 0,
        )
    )

    if old_text == "":
        assert result.occurrences_replaced == 0
        assert test_file.read_text() == new_text
    else:
        assert result.occurrences_replaced == 1
        assert new_text in test_file.read_text()


# Parametrized tests for error conditions
@pytest.mark.parametrize(
    ("initial_content", "old_text", "new_text", "expected_occurrences", "error_match"),
    [
        ("Hello", "Hi", "Hey", 1, "old_text not found"),
        ("Hello Hello", "Hello", "Hi", 1, "Expected 1 occurrence.*but found 2"),
        ("Hello", "Hello", "Hi", 2, "Expected 2 occurrence.*but found 1"),
        ("", "something", "else", 1, "not found"),
        ("Test", "test", "TEST", 1, "old_text not found"),  # Case sensitivity
    ],
)
async def test_edit_file_errors_parametrized(
    local_runtime: LocalRuntime,
    test_file: Path,
    initial_content: str,
    old_text: str,
    new_text: str,
    expected_occurrences: int,
    error_match: str,
):
    """Parametrized tests for various error conditions."""
    if initial_content:
        test_file.write_text(initial_content)

    with pytest.raises((ValueError, FileNotFoundError), match=error_match):
        await local_runtime.edit_file(
            EditFileRequest(
                path=str(test_file),
                old_text=old_text,
                new_text=new_text,
                expected_occurrences=expected_occurrences,
            )
        )


async def test_edit_file_empty_file(local_runtime: LocalRuntime, test_file: Path):
    """Test editing an empty file."""
    test_file.write_text("")

    # Should fail because file exists (can't create)
    with pytest.raises(ValueError, match="file already exists"):
        await local_runtime.edit_file(
            EditFileRequest(path=str(test_file), old_text="", new_text="content", expected_occurrences=1)
        )


async def test_edit_file_deletion(local_runtime: LocalRuntime, test_file: Path):
    """Test replacing text with empty string (deletion)."""
    test_file.write_text("Hello World Goodbye")

    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text=" World", new_text="", expected_occurrences=1)
    )

    assert result.occurrences_replaced == 1
    assert test_file.read_text() == "Hello Goodbye"


async def test_edit_file_no_trailing_newline(local_runtime: LocalRuntime, test_file: Path):
    """Test editing file without trailing newline."""
    test_file.write_text("Line1\nLine2")  # No trailing newline

    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text="Line2", new_text="Modified", expected_occurrences=1)
    )

    assert result.occurrences_replaced == 1
    assert test_file.read_text() == "Line1\nModified"


async def test_edit_file_only_whitespace_old_text(local_runtime: LocalRuntime, test_file: Path):
    """Test with whitespace-only old_text."""
    test_file.write_text("Hello    World")

    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text="    ", new_text=" ", expected_occurrences=1)
    )

    assert result.occurrences_replaced == 1
    assert test_file.read_text() == "Hello World"


async def test_edit_file_whole_file_replacement(local_runtime: LocalRuntime, test_file: Path):
    """Test replacing entire file content."""
    original = "Line1\nLine2\nLine3"
    test_file.write_text(original)

    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text=original, new_text="Completely new", expected_occurrences=1)
    )

    assert result.occurrences_replaced == 1
    assert test_file.read_text() == "Completely new"


async def test_edit_file_case_sensitive(local_runtime: LocalRuntime, test_file: Path):
    """Verify that matching is case-sensitive."""
    test_file.write_text("Hello hello HELLO")

    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text="hello", new_text="hi", expected_occurrences=1)
    )

    assert result.occurrences_replaced == 1
    assert test_file.read_text() == "Hello hi HELLO"


async def test_edit_file_many_occurrences(local_runtime: LocalRuntime, test_file: Path):
    """Test replacing many occurrences."""
    content = "\n".join([f"Line {i}: TODO" for i in range(100)])
    test_file.write_text(content)

    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text="TODO", new_text="DONE", expected_occurrences=100)
    )

    assert result.occurrences_replaced == 100
    assert "TODO" not in test_file.read_text()
    assert test_file.read_text().count("DONE") == 100


async def test_edit_file_overlapping_text(local_runtime: LocalRuntime, test_file: Path):
    """Test with potentially overlapping patterns."""
    test_file.write_text("aaa")

    # Should find exactly 1 match of "aaa", not 3 overlapping matches
    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text="aaa", new_text="b", expected_occurrences=1)
    )

    assert result.occurrences_replaced == 1
    assert test_file.read_text() == "b"


async def test_edit_file_multiline_with_blank_lines(local_runtime: LocalRuntime, test_file: Path):
    """Test multiline replacement including blank lines."""
    test_file.write_text(
        """def func():

    pass

def other():
    pass"""
    )

    result = await local_runtime.edit_file(
        EditFileRequest(
            path=str(test_file),
            old_text="""def func():

    pass""",
            new_text="""def func():
    return True""",
            expected_occurrences=1,
        )
    )

    assert result.occurrences_replaced == 1
    assert "return True" in test_file.read_text()


async def test_edit_file_indentation_preserved(local_runtime: LocalRuntime, test_file: Path):
    """Test that indentation is preserved correctly."""
    test_file.write_text(
        """class MyClass:
    def method(self):
        if True:
            print("hello")
            return True"""
    )

    result = await local_runtime.edit_file(
        EditFileRequest(
            path=str(test_file),
            old_text="""        if True:
            print("hello")
            return True""",
            new_text="""        if True:
            print("goodbye")
            return False""",
            expected_occurrences=1,
        )
    )

    assert result.occurrences_replaced == 1
    final = test_file.read_text()
    assert 'print("goodbye")' in final
    assert "return False" in final


async def test_edit_file_exact_matches_preferred(local_runtime: LocalRuntime, test_file: Path):
    """Test that exact matches are found when they exist."""
    test_file.write_text("Hello\nGoodbye\nHello")

    # This should match the two "Hello" instances
    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text="Hello", new_text="Hi", expected_occurrences=2)
    )

    assert result.occurrences_replaced == 2
    assert test_file.read_text() == "Hi\nGoodbye\nHi"


async def test_edit_file_fuzzy_only_when_no_exact(local_runtime: LocalRuntime, test_file: Path):
    """Test fuzzy matching only kicks in when exact match fails."""
    # Only trimmed version, no exact match
    test_file.write_text("  Hello  ")

    # Fuzzy match should find it
    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text="Hello", new_text="Hi", expected_occurrences=1)
    )

    assert result.occurrences_replaced == 1
    assert "Hi" in test_file.read_text()


async def test_edit_file_diff_format(local_runtime: LocalRuntime, test_file: Path):
    """Verify diff output is in proper unified diff format."""
    test_file.write_text("Line 1\nLine 2\nLine 3")

    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text="Line 2", new_text="Modified Line", expected_occurrences=1)
    )

    # Check diff format
    assert "---" in result.diff or "+++" in result.diff or "@@ " in result.diff
    assert "Line 2" in result.diff or "-Line 2" in result.diff
    assert "Modified Line" in result.diff or "+Modified Line" in result.diff


async def test_edit_file_long_lines(local_runtime: LocalRuntime, test_file: Path):
    """Test editing files with very long lines."""
    long_line = "x" * 10000
    test_file.write_text(f"Start {long_line} End")

    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text=long_line, new_text="short", expected_occurrences=1)
    )

    assert result.occurrences_replaced == 1
    assert "short" in test_file.read_text()
    assert long_line not in test_file.read_text()


async def test_edit_file_consecutive_replacements(local_runtime: LocalRuntime, test_file: Path):
    """Test multiple consecutive edits on the same file."""
    test_file.write_text("Step 0")

    for i in range(1, 5):
        result = await local_runtime.edit_file(
            EditFileRequest(
                path=str(test_file),
                old_text=f"Step {i - 1}",
                new_text=f"Step {i}",
                expected_occurrences=1,
            )
        )
        assert result.occurrences_replaced == 1

    assert test_file.read_text() == "Step 4"


async def test_edit_file_with_regex_special_chars(local_runtime: LocalRuntime, test_file: Path):
    """Test that regex special characters are treated literally."""
    test_file.write_text("Price: $100 (.*)")

    result = await local_runtime.edit_file(
        EditFileRequest(path=str(test_file), old_text="$100 (.*)", new_text="$200 (fixed)", expected_occurrences=1)
    )

    assert result.occurrences_replaced == 1
    assert "$200 (fixed)" in test_file.read_text()
