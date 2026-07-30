# Export performance budgets

The deterministic scale fixtures cover 100,000 and 500,000 asset identities.
They assert structural budgets rather than machine-specific wall-clock limits.

| Measure | Budget |
| --- | --- |
| Manifest synchronization | At most `ceil(changed records / 128)` |
| Work repeated after a crash | At most one unacknowledged group (128 records) |
| Verified-result queue | At most 256 pending records |
| Membership task creation | At most configured concurrency; jobs remain in a bounded queue |
| Fixture peak Python memory | Less than 8 MiB while streaming 500,000 identities |
| Redirected progress volume | One sampled line per 500 outcomes plus phase/final lines |
| Active history | Rotated at 100,000 records or 128 MiB by default |

The live throughput and ETA are evidence, not pass/fail thresholds: filesystem,
network, server, and media sizes dominate them. Scheduled real-storage runs
should capture the rotating logfile, `export-report.txt`, history generation
metadata, elapsed time, and observed synchronization count for comparison.
