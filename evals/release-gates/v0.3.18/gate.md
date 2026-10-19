## phase_eval gate

`before` → `after`, 168 rows.

|metric|before|after|
|---|---:|---:|
|author_required_read_rate|1.000|1.000|
|author_pass_rate|1.000|1.000|
|review_correct_rate|0.977|1.000|
|defect_detection_rate|1.000|1.000|
|false_request_changes_rate|0.009|0.000|
|reviewer_read_rate|1.000|1.000|
|invalid_rate|0.012|0.024|
|author_architecture_lint_pass_rate|1.000|1.000|
|author uncached_input token ratio||0.983|
|author output token ratio||0.982|
|review uncached_input token ratio||1.006|
|review output token ratio||0.995|

### Review scoring coverage

|count|before|after|
|---|---:|---:|
|review completed|48|48|
|review valid|47|46|
|review invalid|1|2|
|review accuracy scored|43|42|
|review accuracy unscored (expected=null)|4|4|
|review correct / accuracy scored|42/43|42/42|

Valid expected=null units remain in completion, token and reading measurements; they are excluded from accuracy and clean-verdict false-positive denominators.

|kit|unscored combination|valid units|case reason|
|---|---|---:|---|
|before|app/team/clean|2|flutter installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|
|before|backend/team/clean|2|python installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|
|after|app/team/clean|2|flutter installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|
|after|backend/team/clean|2|python installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|

All thresholds met.
