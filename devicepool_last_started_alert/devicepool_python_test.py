import time

from systemd import journal

# THINGS TO INSTALL TO MAKE WORK:
#   apt install libsystemd-dev gcc python-dev python3-dev pkg-config


j = journal.Reader()

j.this_boot()
j.this_machine()

j.add_match(_SYSTEMD_UNIT="bitbar.service")

yesterday = time.time() - 24 * 60**2
five_minutes_ago = time.time() - 5 * 60
j.seek_realtime(five_minutes_ago)

# Important! - Discard old journal entries
j.get_previous()

while True:
    i = j.get_next()
    if i:
        print(i["MESSAGE"])
    else:
        break
