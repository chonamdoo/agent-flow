# Journey evaluation

A journey is an XML test whose `<actions>` are the source of truth:

```xml
<journey name="Search and open">
  <description>Open the first matching item.</description>
  <actions>
    <action>Search for tea</action>
    <action>Tap the first search result</action>
    <action>Verify that the details screen is visible</action>
  </actions>
</journey>
```

## Evaluation contract

1. Parse the journey name and ordered actions.
2. Evaluate actions sequentially and exactly as written.
3. Split one action containing multiple operations into ordered sub-actions.
4. Stop on the first failure, app exit, crash, or freeze.
5. Mark remaining actions `SKIPPED`; do not improvise a workaround.

An interaction action succeeds when the specified interaction can be performed
and the app does not crash or behave unexpectedly. Do not add assertions that
the action did not request.

An action beginning with `check` or `verify` is observational. Inspect the
current screen without scrolling or interacting. Every expectation in the
action must hold. If any is false, the action fails.

If an action does not describe a UI interaction or an observable expectation,
report the journey as malformed and stop. The goal is evaluation, not debugging;
reserve fix suggestions for the final summary.

## Result schema

```json
{
  "journey": "Search and open",
  "results": [
    {
      "action": "Search for tea",
      "status": "PASSED",
      "commands": [
        "adb -s emulator-5554 shell input tap 220 180",
        "adb -s emulator-5554 shell input text tea"
      ],
      "comment": "Search results became visible."
    },
    {
      "action": "Verify that the details screen is visible",
      "status": "FAILED",
      "commands": ["android layout --device=emulator-5554 --pretty"],
      "comment": "The search results screen remained visible."
    }
  ]
}
```

Use only `PASSED`, `FAILED`, or `SKIPPED`. Preserve the complete action text,
record every input command, and give concrete observed evidence for failures.
