import pathlib
import subprocess


def test_bin_missing_workers():
    parent_dir = pathlib.Path(__file__).parent
    root_dir = parent_dir / ".."

    # Generate command
    cmd = ["./missing_workers.py", "-h"]

    # excute script
    pipe = subprocess.run(cmd, cwd=root_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert pipe.returncode == 0
