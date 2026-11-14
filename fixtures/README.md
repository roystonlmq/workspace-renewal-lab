# Synthetic session history

These two files contain invented sessions for separate synthetic customers. Each
includes a clear boundary and a resumed request. No real customer content appears.

The regression `test_history_fixtures.py` copies them into a temporary directory,
uses a synthetic clock, then verifies four isolated batches from two sessions and
zero repeated deliveries after restart. It never connects to a remote service.
In normal operation the sink must additionally verify an explicit mapping from
each session to one namespace and scope. A session identity is not a customer scope.
