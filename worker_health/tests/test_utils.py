import string
from urllib.error import URLError

import pytest
import requests

from worker_health import utils


def test_date_in_past():
    from datetime import datetime, timedelta, UTC

    past_date = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future_date = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    assert utils.date_in_past(past_date) is True
    assert utils.date_in_past(future_date) is False


def test_consecutive_non_ones_from_end():
    test_data = [1, 0, 1, 1, 1, 0]  # should be 1
    test_data_2 = [1, 0, 0, 0]  # should be 3s

    assert utils.consecutive_non_ones_from_end(test_data) == 1
    assert utils.consecutive_non_ones_from_end(test_data_2) == 3


def test_graph_percentage():
    assert utils.graph_percentage(0.5) == "[=====     ]"
    assert utils.graph_percentage(1) == "[==========]"
    assert utils.graph_percentage(0) == "[          ]"


def test_fetch_url():
    url, res, exc = utils.fetch_url("http://badurl.comaaa/bad")
    assert isinstance(exc, URLError)


def test_get_jsonc2():
    with pytest.raises(requests.exceptions.ConnectionError):
        url, res, exc = utils.get_jsonc2("http://badurl.comaaa/bad")


def test_arr_get_followers():
    # most basic
    test_arr = ["a"]
    assert utils.arr_get_followers(test_arr, "a", 0) == ["a"]

    assert utils.arr_get_followers(test_arr, "a", 1)

    # item not in array
    with pytest.raises(Exception):
        utils.arr_get_followers(test_arr, "c", 2, raise_on_errors=True)
    # in default no raise mode
    utils.arr_get_followers(test_arr, "c", 2) == []

    # ['a', 'b', 'c', ...]
    test_arr = list(string.ascii_lowercase)
    assert utils.arr_get_followers(test_arr, "c", 2) == ["c", "d", "e"]

    # if requested past length of array, return what we can
    assert utils.arr_get_followers(test_arr, "z", 10) == ["z"]


def test_arr_get_slice_from_item():
    test_arr = ["a"]
    assert utils.arr_get_slice_from_item(test_arr, "a") == ["a"]

    test_arr = list(string.ascii_lowercase)
    assert utils.arr_get_slice_from_item(test_arr, "w") == ["w", "x", "y", "z"]
