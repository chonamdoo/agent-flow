## phase_eval gate

`before` → `after`, 168 rows.

|metric|before|after|
|---|---:|---:|
|author_required_read_rate|1.000|1.000|
|author_pass_rate|1.000|1.000|
|review_correct_rate|1.000|1.000|
|defect_detection_rate|1.000|1.000|
|false_request_changes_rate|0.000|0.000|
|reviewer_read_rate|1.000|1.000|
|invalid_rate|0.000|0.000|
|author_architecture_lint_pass_rate|1.000|1.000|
|author uncached_input token ratio||0.815|
|author output token ratio||0.970|
|review uncached_input token ratio||0.849|
|review output token ratio||0.942|

### Review scoring coverage

|count|before|after|
|---|---:|---:|
|review completed|48|48|
|review valid|48|48|
|review invalid|0|0|
|review accuracy scored|44|44|
|review accuracy unscored (expected=null)|4|4|
|review correct / accuracy scored|44/44|44/44|

Valid expected=null units remain in completion, token and reading measurements; they are excluded from accuracy and clean-verdict false-positive denominators.

|kit|unscored combination|valid units|case reason|
|---|---|---:|---|
|before|app/team/clean|2|flutter installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|
|before|backend/team/clean|2|python installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|
|after|app/team/clean|2|flutter installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|
|after|backend/team/clean|2|python installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|

All thresholds met.
