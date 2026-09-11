# Tests

Run the tests in a Linux Python environment supported by Home Assistant:

```sh
python -m pip install -r requirements-test.txt
python -m pytest
```

The test plugin installs a compatible Home Assistant version. Tests mock UDP
requests and exercise actual config flows, setup/reload, setup retries, device
and entity registries, migration, and command routing between two batteries.
