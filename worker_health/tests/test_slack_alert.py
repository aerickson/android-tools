import pathlib
import subprocess


def test_bin_slack_alert():
    parent_dir = pathlib.Path(__file__).parent
    root_dir = parent_dir / ".."

    # Generate command
    cmd = ["./slack_alert.py", "-h"]

    # excute script
    pipe = subprocess.run(cmd, cwd=root_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert pipe.returncode == 0
