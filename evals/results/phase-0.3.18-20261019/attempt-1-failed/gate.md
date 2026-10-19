## phase_eval gate

`before` → `after`, 168 rows.

|metric|before|after|
|---|---:|---:|
|author_required_read_rate|1.000|1.000|
|author_pass_rate|1.000|1.000|
|review_correct_rate|0.921|0.895|
|defect_detection_rate|1.000|1.000|
|false_request_changes_rate|0.083|0.083|
|reviewer_read_rate|1.000|1.000|
|invalid_rate|0.071|0.083|
|author_architecture_lint_pass_rate|1.000|1.000|
|author uncached_input token ratio||1.110|
|author output token ratio||0.953|
|review uncached_input token ratio||1.029|
|review output token ratio||0.997|

### Review scoring coverage

|count|before|after|
|---|---:|---:|
|review completed|48|48|
|review valid|42|42|
|review invalid|6|6|
|review accuracy scored|38|38|
|review accuracy unscored (expected=null)|4|4|
|review correct / accuracy scored|35/38|34/38|

Valid expected=null units remain in completion, token and reading measurements; they are excluded from accuracy and clean-verdict false-positive denominators.

|kit|unscored combination|valid units|case reason|
|---|---|---:|---|
|before|app/team/clean|2|flutter installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|
|before|backend/team/clean|2|python installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|
|after|app/team/clean|2|flutter installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|
|after|backend/team/clean|2|python installs pending by default; pending mode lets a reviewer ask for an architecture decision on a new module, so approve is not a fixed answer|

### Failures
- review_correct_rate 0.895 is below floor 0.95
- review_correct_rate dropped: 0.921 → 0.895
- author uncached_input token ratio 1.110 exceeds 1.05
