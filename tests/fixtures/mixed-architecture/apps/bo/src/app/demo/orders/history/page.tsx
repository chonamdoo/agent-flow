import { SectionTitle } from "../_components/section-title";
import { historyEntries } from "./_lib/history";

export default function HistoryPage() {
  return (
    <main>
      <SectionTitle title="Synthetic order history" />
      <ul>
        {historyEntries.map((entry) => (
          <li key={entry.reference}>{entry.reference}: {entry.status}</li>
        ))}
      </ul>
    </main>
  );
}
