import os

import git
import yaml
from git.exc import GitCommandError


class DevicepoolConfig:
    # TODO: make health.py use this class (currently manages it's own devicepool checkout)
    def __init__(self):
        self.repo_url = "https://github.com/mozilla-platform-ops/mozilla-bitbar-devicepool.git"
        self.repo_path = os.path.expanduser("~/.cache/worker_health/mozilla-bitbar-devicepool")

        self.device_group_devices = {}
        self.configured_devices = {}

        self.clone_or_update_repo()
        self.parse_config_file()

    def clone_or_update_repo(self):
        try:
            if not os.path.exists(self.repo_path):
                # Create directory if it doesn't exist
                os.makedirs(os.path.dirname(self.repo_path), exist_ok=True)
                # Clone the repository
                git.Repo.clone_from(self.repo_url, self.repo_path)
            else:
                # Pull latest changes if repo exists
                repo = git.Repo(self.repo_path)
                origin = repo.remotes.origin
                origin.pull()
        except GitCommandError as e:
            print(f"Git operation failed: {e}")
            raise

    def parse_config_file(self):
        config_file_path = os.path.join(self.repo_path, "config", "config.yml")
        try:
            with open(config_file_path, "r") as file:
                config_data = yaml.safe_load(file)
        except FileNotFoundError:
            print(f"Config file not found: {config_file_path}")
        except yaml.YAMLError as e:
            print(f"Error parsing YAML file: {e}")

        for device_group in config_data["device_groups"]:
            # if the project contains 'test' or 'builder', skip it
            if "test" in device_group or "builder" in device_group:
                continue
            if config_data["device_groups"][device_group]:
                self.device_group_devices[device_group] = list(config_data["device_groups"][device_group].keys())

        # iterate over the key,value pairs in config_data["projects"]
        for project_name, value in config_data["projects"].items():
            if project_name != "defaults":
                tc_worker_type = value["additional_parameters"]["TC_WORKER_TYPE"]
                if value["device_group_name"] in self.device_group_devices:
                    self.configured_devices[tc_worker_type] = self.device_group_devices[value["device_group_name"]]

    def get_configured_devices(self):
        return self.configured_devices
