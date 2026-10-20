# Exploratory QA

- Write a short charter per changed flow: who the user is, what they try, what would hurt them.
- Spend a bounded effort per charter; stop when it is used, not when nothing has been found.
- Every suspected defect becomes a reproduction: exact steps or a command. Run it. It is a confirmed finding
  only if it fails against this change; otherwise list it as unverified with what you tried.
- Exploratory findings can only add failures. They never turn a missing automated check into a pass.
