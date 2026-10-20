# Evidence

- Keep the report the runner wrote (JSON, JUnit), and the screenshots or traces of failures. List their paths.
- Count from the report, not the exit code: executed, passed, failed, skipped, flaky. Zero executed, only
  skipped, or any flaky test is not a pass (see the main rules).
- Mask tokens, passwords, cookies and personal data in anything you record; do not attach secrets.
- If the report cannot be read, the run has no verdict: report it as such instead of guessing.
