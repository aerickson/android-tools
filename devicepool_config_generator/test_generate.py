import pytest


@pytest.fixture
def generate_obj():
    import generate

    return generate.DevicePoolConfigGenerator()


@pytest.fixture
def dgs():
    return {
        "motog4-docker-builder-2": {"Docker Builder": None},
        "motog5-batt": None,
        "motog5-batt-2": None,
        "motog5-perf": None,
        "motog5-perf-2": {
            "motog5-14": None,
            "motog5-15": None,
            "motog5-16": None,
            "motog5-17": None,
            "motog5-18": None,
            "motog5-19": None,
            "motog5-38": None,
            "motog5-39": None,
        },
        "motog5-test": None,
        "motog5-unit": None,
        "motog5-unit-2": None,
        "pixel2-batt": None,
        "pixel2-batt-2": None,
        "pixel2-perf": None,
        "pixel2-perf-2": {"pixel2-57": None, "pixel2-58": None, "pixel2-59": None},
        "pixel2-unit": None,
        "pixel2-unit-2": {
            "pixel2-12": None,
            "pixel2-13": None,
            "pixel2-14": None,
            "pixel2-15": None,
        },
        "test-1": {"motog5-40": None},
        "test-2": {"pixel2-60": None},
        "test-3": None,
    }


# # content of test_sample.py
# def inc(x):
#     return x + 1

# def test_answer():
#     assert inc(3) == 5


def test_split_list(generate_obj):
    a_list = [
        "motog5-28",
        "motog5-29",
        "motog5-30",
        "motog5-37",
        "motog5-38",
        "motog5-39",
    ]
    expected = ["motog5-28"]
    assert generate_obj.split_list(a_list, slice_start=0, slice_end=1 / 6) == expected


def test_split_dict_based_on_device(generate_obj):
    input = {"motog5-perf-2": 0, "pixel2-perf-2": 305, "pixel2-unit-2": 0}
    expected = {
        "motog5": {"motog5-perf-2": 0},
        "pixel2": {"pixel2-perf-2": 305, "pixel2-unit-2": 0},
    }
    assert generate_obj.split_dict_based_on_device(input) == expected


def test_device_type_from_string(generate_obj):
    assert generate_obj.device_type_from_string("asdf p2 asdfasd") == "pixel2"
    assert generate_obj.device_type_from_string("asdf g5 asdfasd") == "motog5"
    with pytest.raises(Exception):
        generate_obj.device_type_from_string("asdf asdfasd")


def test_queue_type_from_string(generate_obj):
    assert generate_obj.queue_type_from_string("asdf perf asdfasd") == "perf"
    assert generate_obj.queue_type_from_string("asdf unit asdfasd") == "unit"
    assert generate_obj.queue_type_from_string("asdf batt asdfasd") == "batt"
    assert generate_obj.queue_type_from_string("asdf builder asdfasd") == "test"
    assert generate_obj.queue_type_from_string("asdf test asdfasd") == "test"
    with pytest.raises(Exception):
        generate_obj.device_type_from_string("asdf asdfasd")


def test_extract_devices_from_device_groups(generate_obj, dgs):
    res = generate_obj.extract_devices_from_device_groups(dgs, ["pixel2-perf-2", "pixel2-unit-2", "motog5-perf-2"])
    expected = {
        "motog5": {
            "motog5-14",
            "motog5-15",
            "motog5-16",
            "motog5-17",
            "motog5-18",
            "motog5-19",
            "motog5-38",
            "motog5-39",
        },
        "pixel2": {
            "pixel2-12",
            "pixel2-13",
            "pixel2-14",
            "pixel2-15",
            "pixel2-57",
            "pixel2-58",
            "pixel2-59",
        },
    }
    assert res == expected
