import pytest

from dbgtool.cli import extract_python_script_path, is_simple_python_command


def test_pytest_command_not_python_mode_and_no_script_extracted():
    cmd = "pytest -s -rA /testbed/tests/test_module.py::TestClass::test_method"
    assert is_simple_python_command(cmd) is False
    assert extract_python_script_path(cmd) is None


def test_simple_python_command_detected_and_script_extracted():
    cmd = "python /testbed/django/contrib/admin/options.py --output a.py"
    assert is_simple_python_command(cmd) is False
    assert extract_python_script_path(cmd).endswith("/testbed/django/contrib/admin/options.py")


def test_compound_command_extracts_last_python_script_and_not_simple():
    cmd = "cd /testbed/django && echo start && pytest -s -rA /testbed/tests/test_module.py::TestClass::test_method"
    assert is_simple_python_command(cmd) is False
    assert extract_python_script_path(cmd) is None