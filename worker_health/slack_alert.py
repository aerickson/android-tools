#!/usr/bin/env python3

import argparse
import os
import pprint
import random
import time

import requests
import schedule
import toml

from worker_health.health import Health, logger, utils


class SlackAlert:
    def __init__(self, log_level, time_limit, testing_mode_enabled):
        self.time_limit = time_limit
        self.log_level = log_level
        self.alerting_enabled = False
        self.testing_mode = testing_mode_enabled
        self.bitbar_tz = "America/Los_Angeles"

        self.minutes_between_failure_retry = 5

        # config file
        self.configuration_file = os.path.join(os.path.expanduser("~"), ".bitbar_slack_alert.toml")
        self.toml = self.read_toml()

        # wh instance
        self.wh = Health(self.log_level)

        # webhook url
        if self.get_toml_value("webhook_url"):
            self.webhook_url = self.toml["main"]["webhook_url"]
            self.alerting_enabled = True

    def set_toml_value(self, key, value):
        self.toml["main"][key] = value
        self.write_toml(self.toml)

    def get_toml_value(self, key):
        if "main" in self.toml and key in self.toml["main"]:
            return self.toml["main"][key]
        return False

    def write_toml(self, dict_to_write):
        with open(self.configuration_file, "w") as writer:
            toml.dump(dict_to_write, writer)

    def read_toml(self):
        if os.path.exists(self.configuration_file):
            return toml.load(self.configuration_file)
        else:
            # first run
            default_doc = """
[main]
webhook_url = ""
currently_alerting = false
          """
            return_dict = toml.loads(default_doc)
            self.write_toml(return_dict)
            return return_dict

    def slack_alert(self):
        wh = self.wh
        # for slack alerts, don't mention tc quarantined hosts
        # - will still appear if offline in devicepool
        report_data = wh.get_slack_report(show_all=False, time_limit=self.time_limit, verbosity=self.log_level)

        if args.log_level:
            logger.debug(pprint.pformat(report_data))

        # if we find bad hosts, double check before continuing (and alerting)
        if len(report_data["union"]) > 0:
            print(f"found bad hosts, rechecking in {self.minutes_between_failure_retry} minutes...")
            time.sleep(self.minutes_between_failure_retry * 60)
            report_data = wh.get_slack_report(show_all=False, time_limit=self.time_limit, verbosity=self.log_level)

        if len(report_data["union"]) > 0:
            # update state indicating we're alerting
            self.set_toml_value("currently_alerting", True)

            # bold workers with high confidence (devicepool showing as bad)
            worker_string = "["
            for worker in report_data["union"]:
                # bold the device if it's offline in the devicepool api
                if worker in report_data["devicepool"]:
                    worker_string += "*%s*, " % worker
                else:
                    worker_string += "%s, " % worker
            worker_string = worker_string[:-2]
            worker_string += "]"

            message = "problem workers (%s): %s" % (
                len(report_data["union"]),
                worker_string,
            )
            if self.alerting_enabled:
                self.send_slack_message(message)
            else:
                logger.info("would have sent message: '%s'" % message)
        else:
            # if we were alerting previously, mention that we're all good now
            if self.get_toml_value("currently_alerting"):
                logger.info("sending all clear message")
                emoji_options = [
                    ":tada:",
                    ":grin:",
                    ":grinning:",
                    ":thumbsup:",
                    ":smile:",
                    ":smiley:",
                ]
                message = "all device issues resolved %s" % (random.choice(emoji_options))
                if self.alerting_enabled:
                    self.send_slack_message(message)
                else:
                    logger.info("would have sent message: '%s'" % message)
            logger.info("no problem workers")
            self.set_toml_value("currently_alerting", False)

    # TODO: if alerting is not enabled, just mention we'd send a message
    def send_slack_message(self, message):
        # cli example:
        #   curl -X POST -H 'Content-type: application/json' \
        #      --data '{"text":"Hello, World!"}' WEBHOOK_URL
        data = {"text": message}
        retries = 2
        while retries >= 0:
            r = requests.post(url=self.webhook_url, json=data)
            if r.status_code == 200:
                break
            logger.info("got a non-200 status code, retrying...")
            retries -= 1

        if r.status_code == 200:
            logger.info("slack message sent. message: '%s'" % message)
        else:
            logger.error("failure when trying to send slack message")
            logger.error(r.text)
            logger.error(r.status_code)

    def main(self, args):
        if self.alerting_enabled:
            logger.info("alerting enabled!")
        else:
            logger.warning("alerting _not_ enabled. please edit '%s' and rerun." % self.configuration_file)

        # warn the user that all possible data is not available
        utils.bitbar_systemd_service_present(warn=True)

        if args.testing_mode:
            testing_mode_start_delay = 10
            logger.warning("testing mode enabled!messages will still be sent if webhook_url configured.")
            if testing_mode_start_delay:
                logger.warning("starting in %s seconds..." % testing_mode_start_delay)
                time.sleep(testing_mode_start_delay)

        if self.testing_mode:
            minutes_to_run = 10
            logger.info("job will run every %s minutes" % minutes_to_run)
            # run one right now
            logger.info("running once immediately")
            self.slack_alert()
            # test schedule
            schedule.every(minutes_to_run).minutes.do(self.slack_alert)
        else:
            # run once every hour at specific minute
            minute_of_hour_to_run = 7
            minute_at_string = ":%s" % str(minute_of_hour_to_run).zfill(2)
            logger.info("job will run every hour at %s" % minute_at_string)
            schedule.every().hour.at(minute_at_string).do(self.slack_alert)

        while True:
            schedule.run_pending()
            time.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        dest="log_level",
        default=0,
        help="specify multiple times for even more verbosity",
    )
    parser.add_argument(
        "-t",
        "--time-limit",
        type=int,
        default=95,
        help=("for tc, devices are missing if not reporting for longer than this many minutes. defaults to 95."),
    )
    parser.add_argument("--testing-mode", action="store_true", default=False, help="enable testing mode")
    args = parser.parse_args()

    sa = SlackAlert(args.log_level, args.time_limit, args.testing_mode)
    sa.main(args)
