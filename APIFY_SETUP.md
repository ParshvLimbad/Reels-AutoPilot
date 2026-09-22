# Apify discovery on the Pi

The worker uses Apify for profile discovery, the public reel downloader for video files, and the official Instagram API for publishing. Credentials are stored only in `.official/apify-keys.json` (ignored by Git, mode 0600 on Pi).

Each run selects the next configured source profile, requests up to 10 latest non-pinned reels, and reserves its budget before starting. Runs occur at most every two hours, alternating keys by reserved monthly usage. Duplicate results are discarded before downloading. Paid transcript, shares and video-download extras are disabled.

Limits: 60 requested results/key/day, 1800/key/calendar month, and $0.026 maximum Actor charge/run. This caps this worker's Actor runs at $4.68/key/calendar month; storage, other Apify usage, and account billing periods are separate. The verified accounts are Free plans with a $5 usage ceiling. No plan upgrades or purchases are performed. Counts are conservative reservations, not actual invoices or guaranteed unique reels.

An ambiguous start pauses discovery (`uncertain: true`) rather than risk duplicate charges. Check the Apify console to recover the run ID before clearing that state. Existing downloads can continue publishing. State and reservations survive restarts in `.official/apify-state.json`; they appear under discovery on `/official`.

The first live runs returned 9 results each. The pipeline downloaded discovered videos successfully and confirmed publication through the official API. Instagram reported a 100-post rolling 24-hour quota; the Pi interval is 15 minutes. Publication also checks Meta's current quota before submitting.

Limitations: latest-10 discovery can return already-seen reels and does not guarantee enough unique content for continuous posting. Quick Tunnel hosting remains temporary and can be interrupted. Do not delete delivery records or reset posted reels to refill the queue.
