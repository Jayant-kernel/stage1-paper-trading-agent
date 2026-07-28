# Local append-only evidence

Runtime data is excluded from Git. The application creates these paths as
needed:

```text
raw/ticks/date=YYYY-MM-DD/symbol=.../
raw/news/date=YYYY-MM-DD/source=.../
derived/bars/date=YYYY-MM-DD/
derived/features/date=YYYY-MM-DD/
derived/candidates/date=YYYY-MM-DD/
decisions/date=YYYY-MM-DD/
reports/
run_cards/
evaluation/development/
evaluation/validation/
evaluation/sealed/
```

Raw and derived partitions are never overwritten. Corrections create a new
version and manifest.
