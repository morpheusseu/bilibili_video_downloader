# Headless Bilibili client

`bilibili-client` is an independently installable pure Python package shared by
the desktop application and the Boxy connector. It pins bpi-py, HTTP and Broadcast
dependencies; it imports neither Qt, FFmpeg, the desktop application nor Boxy.

The client owns browser-session validation, provider response parsing, pagination,
request pacing and Broadcast transport. `Bilibili()` performs no image requests
during authorization. Only the desktop explicitly enables `fetch_avatar=True`.
Cover/image downloads are explicit calls, never a side effect of content reads.

`browser_login.chrome_login` opens installed Chrome with an isolated temporary
profile. It returns verified Cookie/UA material without saving it. The consumer
owns credential storage, cancellation, admission authority and publication.

`AccountCache` and the view-oriented readers support the desktop's bounded cache.
They are not a connector evidence archive or a proof of completed time coverage.
A connector uses its own transactional records, acquisition cursors and receipts.
Neither consumer reads or writes the other's private state.

From the repository root, `uv sync --frozen` installs the editable client for the
desktop and its tests. Other consumers install this `client/` subproject without
bringing in the application dependencies. Root tests cover the production SDK,
transport, browser handoff and Broadcast path with synthetic external responses.
