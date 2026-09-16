import { formatLabel } from "@/shared/lib/format";
import { accountLabels } from "./_lib/accounts";

export default function AccountsPage() {
  return (
    <main>
      <h1>{formatLabel("Accounts")}</h1>
      <ul>{accountLabels.map((label) => <li key={label}>{label}</li>)}</ul>
    </main>
  );
}
