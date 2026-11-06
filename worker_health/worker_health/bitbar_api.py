import os

from testdroid import Testdroid


class BitbarApiException(Exception):
    pass


class BitbarApiTestdroidEnvvVarsNotSetException(BitbarApiException):
    pass


class BitbarApi:
    def __init__(self):
        TESTDROID_URL = os.environ.get("TESTDROID_URL")
        TESTDROID_APIKEY = os.environ.get("TESTDROID_APIKEY")
        if TESTDROID_URL and TESTDROID_APIKEY:
            # TESTDROID = Testdroid(apikey=TESTDROID_APIKEY, url=TESTDROID_URL)
            self.TESTDROID = Testdroid(apikey=TESTDROID_APIKEY, url=TESTDROID_URL)
        else:
            raise BitbarApiTestdroidEnvvVarsNotSetException(
                "TESTDROID_URL and TESTDROID_APIKEY must be set in the environment",
            )

    def get_device_problems(self):
        """Return list of matching Bitbar devices with device problems.

        :param device_model: string prefix of device names to match.
        """

        path = "admin/device-problems"
        payload = {"limit": 0}
        data = self.TESTDROID.get(path=path, payload=payload)["data"]
        data = [d for d in data if d["deviceName"] != "Docker Builder"]
        return data
