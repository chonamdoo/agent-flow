const HOSTS = new Set(["claude", "codex", "omp"]);

export function detectActiveHost(env = process.env) {
  const override = String(
    env.AGENT_FLOW_ACTIVE_HOST || env.AGENT_FLOW_HOST || "",
  ).trim().toLowerCase();
  if (HOSTS.has(override)) return override;
  if (env.CLAUDECODE || env.CLAUDE_CLI) return "claude";
  if (
    env.CODEX_CLI
    || env.CODEX_SHELL
    || env.CODEX_THREAD_ID
    || env.CODEX_INTERNAL_ORIGINATOR_OVERRIDE
  ) return "codex";
  if (env.OMP_PROFILE || env.PI_CODING_AGENT_DIR) return "omp";
  if (env.CODEX_HOME) return "codex";
  return null;
}
